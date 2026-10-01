"""P0-11 剪辑合成：规划层（选片、时长、台词放置）。只用标准库，不调 ffmpeg 渲染；选片时用 media.probe 校验片段可解析。

输入：ep01（DramaIR）、P0-05 逐句配音目录（l_*.mp3 + asr/）、P0-08 的视频片段目录、P0-07 选定首帧的 manifest。
输出：`Plan`——13 个镜头各自的来源（real / placeholder）、目标时长与台词窗口，供 OTIO 构建（otio_timeline.py）与渲染（render.py）使用。

设计要点：
- **按镜头自动选片**：`<videos>/<shot_id>.mp4` 存在且可解析 → real；不存在 → placeholder（P0-07 选定首帧缓慢推近）；存在但损坏 → 报错，不静默降级。
  不写死镜头清单，补入真实片段后重跑即可。
- **时长与素材真假无关**：镜头目标时长只由 `hint_s` 与台词窗口决定，所以补入真实片段后时间线、音频、字幕时间都不变，只有画面像素变。
- 台词窗口取 ASR 的 start / end（`tts.metrics_for_dir`），在 OTIO 里是对原 mp3 的 source_range（非破坏性）。
- 真实片段短于目标时冻结尾帧补足，每镜头最多延长 MAX_EXTEND_S；超限报错（不加速、不截断台词）。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from poc import media, script, tts

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VIDEOS = ROOT / "docs/reports/p0/P0-08/full-h3/videos"
DEFAULT_TTS_DIR = ROOT / "docs/reports/p0/P0-05/C-instruct-speed"
DEFAULT_KEYFRAMES = ROOT / "docs/reports/p0/P0-07/keyframe-r1/manifest.json"
DEFAULT_OUT = ROOT / "docs/reports/p0/P0-11"

FPS = 24
WIDTH, HEIGHT = 1080, 1920
HEAD_S = 0.15  # 镜头开头到第一句台词剪辑起点
GAP_S = 0.2  # 同一镜头内两句台词之间
TAIL_S = 0.25  # 最后一句台词之后
PAD_BEFORE_S = 0.15  # 台词剪辑起点早于 ASR 语音起点
PAD_AFTER_S = 0.25  # 台词剪辑终点晚于 ASR 语音终点
MAX_EXTEND_S = 2.0  # 真实片段最多冻结尾帧延长的秒数


class ComposeError(Exception):
    """规划失败：输入缺失 / 损坏、台词放不下、延长超限。"""


def frames(seconds: float) -> int:
    """秒 → 帧数（向上取整到整帧，24 fps；浮点误差容忍 1e-6）。"""
    return math.ceil(seconds * FPS - 1e-6)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class LinePlan:
    line_id: str
    speaker: str
    kind: str
    text: str
    mp3: str  # 相对仓库根的路径
    mp3_sha256: str
    src_in_s: float  # 剪辑在 mp3 内的起点
    src_out_s: float
    start_s: float  # 在镜头内的起点
    speech_start_s: float  # 镜头内语音起点 / 终点（字幕时间）
    speech_end_s: float
    file_s: float = 0.0  # 整个 mp3 的时长（OTIO available_range 用）

    @property
    def dur_s(self) -> float:
        return round(self.src_out_s - self.src_in_s, 4)


@dataclass
class ShotPlan:
    shot_id: str
    hint_s: float
    target_frames: int
    start_frames: int  # 在成片时间线上的起点
    source: str  # real / placeholder
    video: str | None  # real 的片段路径（相对仓库根）
    video_sha256: str | None
    video_s: float | None  # real 片段的原生时长
    video_has_audio: bool
    keyframe: str | None  # 占位用的首帧（real 也记录，便于对照）
    keyframe_sha256: str | None
    extend_s: float  # 冻结尾帧延长（real）或比 hint 多出的时长
    trim_s: float  # real 片段比目标长时被裁掉的秒数
    lines: list[LinePlan] = field(default_factory=list)
    sfx: list[str] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)

    @property
    def target_s(self) -> float:
        return round(self.target_frames / FPS, 4)

    @property
    def placeholder(self) -> bool:
        return self.source == "placeholder"


@dataclass
class Plan:
    shots: list[ShotPlan]
    ir_sha256: str
    warnings: list[str] = field(default_factory=list)

    @property
    def total_frames(self) -> int:
        return sum(s.target_frames for s in self.shots)

    @property
    def total_s(self) -> float:
        return round(self.total_frames / FPS, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_s": self.total_s, "shots": [asdict(s) | {"target_s": s.target_s, "placeholder": s.placeholder} for s in self.shots],
            "ir_sha256": self.ir_sha256, "warnings": self.warnings,
        }


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _keyframes(manifest_path: Path) -> dict[str, tuple[Path, str]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out: dict[str, tuple[Path, str]] = {}
    for item in manifest["images"]:
        if item.get("selected"):
            path = manifest_path.parent / item["file"]
            if not path.is_file():
                raise ComposeError(f"首帧不存在：{path}")
            sha = sha256_file(path)
            if item.get("sha256") and item["sha256"] != sha:
                raise ComposeError(f"首帧 sha256 不匹配：{item['shot_id']}")
            out[item["shot_id"]] = (path, sha)
    return out


def plan(
    ir_path: Path = script.SAMPLE_EP01,
    videos_dir: Path = DEFAULT_VIDEOS,
    tts_dir: Path = DEFAULT_TTS_DIR,
    keyframes_manifest: Path = DEFAULT_KEYFRAMES,
    probe: Callable[[Path], Any] = media.probe,
    require_keyframes: bool = True,
) -> Plan:
    """规划 13 个镜头。probe 可注入（单测用）；真实片段必须能 probe，否则报错。"""
    doc, _ = tts.load_episode(ir_path)
    metrics = tts.metrics_for_dir(tts_dir, ir_path)
    if metrics["problems"]:
        raise ComposeError("配音目录有问题：" + "；".join(metrics["problems"]))
    per_line = {m["line_id"]: m for m in metrics["lines"]}
    keyframes = _keyframes(keyframes_manifest) if (require_keyframes or keyframes_manifest.is_file()) else {}
    shots: list[ShotPlan] = []
    warnings: list[str] = []
    cursor = 0
    for shot in tts.shots_of(doc):
        sid = shot["shot_id"]
        hint = float(shot["duration"]["hint_s"])
        lines, offset = [], HEAD_S
        for dlg in shot.get("dialogue", []):
            m = per_line.get(dlg["line_id"])
            if m is None:
                raise ComposeError(f"{sid}：缺少台词 {dlg['line_id']} 的配音")
            src_in = max(0.0, m["speech_start_s"] - PAD_BEFORE_S)
            src_out = min(m["file_s"], m["speech_end_s"] + PAD_AFTER_S)
            mp3 = tts_dir / f"{dlg['line_id']}.mp3"
            line = LinePlan(
                dlg["line_id"], dlg["speaker"], dlg["kind"], dlg["text"], _rel(mp3), m["sha256"], round(src_in, 4), round(src_out, 4),
                round(offset, 4), round(offset + (m["speech_start_s"] - src_in), 4), round(offset + (m["speech_end_s"] - src_in), 4), float(m["file_s"]),
            )
            lines.append(line)
            offset += line.dur_s + GAP_S
        need = (offset - GAP_S + TAIL_S) if lines else 0.0
        target = frames(max(hint, need))
        video_path = videos_dir / f"{sid}.mp4"
        key = keyframes.get(sid)
        if video_path.exists():
            try:
                info = probe(video_path)
            except media.MediaError as exc:
                raise ComposeError(f"{sid}：视频片段存在但不可解析（{exc}）；删除它才会改用占位") from None
            video_s = float(info.duration_s)
            extend = max(0.0, target / FPS - video_s)
            if extend > MAX_EXTEND_S + 1e-9:
                raise ComposeError(f"{sid}：目标 {target / FPS:.2f} 秒比片段 {video_s:.2f} 秒长 {extend:.2f} 秒，超过延长上限 {MAX_EXTEND_S} 秒")
            sp = ShotPlan(
                sid, hint, target, cursor, "real", _rel(video_path), sha256_file(video_path), video_s, bool(info.has_audio),
                _rel(key[0]) if key else None, key[1] if key else None, round(extend, 4), round(max(0.0, video_s - target / FPS), 4),
            )
        else:
            if key is None:
                raise ComposeError(f"{sid}：没有视频片段，也没有 selected 首帧可做占位")
            sp = ShotPlan(sid, hint, target, cursor, "placeholder", None, None, None, False, _rel(key[0]), key[1], round(target / FPS - hint, 4), 0.0)
        sp.lines, sp.sfx = lines, list(shot.get("sfx", []))
        sp.characters = [c.get("character_id", "") for c in shot.get("characters", [])]
        if target / FPS > hint + 1e-9:
            warnings.append(f"{sid}：台词需要 {target / FPS:.2f} 秒，比 hint_s {hint:g} 秒长")
        shots.append(sp)
        cursor += target
    return Plan(shots, hashlib.sha256(Path(ir_path).read_bytes()).hexdigest(), warnings)


def render_table(p: Plan) -> str:
    """`--dry-run` 输出：逐镜头表（来源、hint、目标时长、延长、台词窗口）。"""
    rows = ["镜头\t来源\thint\t目标\t延长\t台词窗口（镜头内起止，秒）"]
    for s in p.shots:
        windows = "；".join(f"{ln.line_id} {ln.start_s:.2f}–{ln.start_s + ln.dur_s:.2f}" for ln in s.lines) or "—"
        rows.append(f"{s.shot_id}\t{s.source}\t{s.hint_s:g}\t{s.target_s:.2f}\t{s.extend_s:.2f}\t{windows}")
    real = sum(1 for s in p.shots if not s.placeholder)
    rows.append(f"合计 {len(p.shots)} 镜头（real {real} / placeholder {len(p.shots) - real}），总时长 {p.total_s:.2f} 秒（{p.total_frames} 帧）")
    return "\n".join(rows)


def manifest_entries(p: Plan) -> list[Mapping[str, Any]]:
    """compose-manifest.json 的逐镜头选择记录。"""
    return [
        {"shot_id": s.shot_id, "source": s.source, "placeholder": s.placeholder, "video": s.video, "video_sha256": s.video_sha256,
         "keyframe": s.keyframe, "keyframe_sha256": s.keyframe_sha256, "target_s": s.target_s, "extend_s": s.extend_s}
        for s in p.shots
    ]
