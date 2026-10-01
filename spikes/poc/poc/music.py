"""音乐与音效（P0-10）：按场景情绪规划 BGM、按镜头标签规划音效、BGM 度量与（有密钥时的）生成。

离线部分（不需要密钥、不联网、不写 runs/）：
- `plan_bgm`：每个场景一段 BGM，时间轴取成片口径（compose.plan）或 hint 口径；接口单次最短 30 秒，所以
  gen_s = max(30, ceil(play_s))，多出的部分合成时裁掉。段间留 XFADE_S 的交叉淡化。
- `sfx_plan`：按规则表把 DramaIR 的 shot.sfx 标签归类（环境铺底 / 拟音 / 提示音 / 人群），未命中的标 uncategorized，不静默丢弃。
- `analyze`：时长偏差、LUFS / 真峰值 / LRA、首尾静音、末 1 秒电平落差，并给出相对对白轨的混音增益建议。
在线部分（缺 VOLC_ACCESSKEY / VOLC_SECRETKEY 时退出码 2；P0-10 离线阶段没有真实调用过）：
- `generate`：每段提交 GenBGMForTime → QuerySong 轮询 → 下载 → 落盘并写 manifest；经 Run.call 记账，node_key 为请求内容 sha256。

BGM 描述与情绪映射只用 DramaIR 已有字段（scene.mood / setting / series），不另建结构（INV-02）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from poc import compose, config, costume, media, pricing, providers, script, tts, volc_sign
from poc.runlog import Run

PROMPT_VERSION = "bgm.v1"
MODEL_VERSION = "v5.0"
MIN_GEN_S = 30
MAX_GEN_S = 120
XFADE_S = 1.0  # 相邻场景 BGM 的交叉淡化长度
TAIL_FADE_S = 1.5  # 最后一段的结尾淡出
SCHEMA_PATH = script.REPO_ROOT / "packages" / "drama-ir" / "schema" / "v0" / "drama.schema.json"
DEFAULT_REFERENCE = script.REPO_ROOT / "docs" / "reports" / "p0" / "P0-11" / "audio" / "ambient-off.m4a"
BELOW_DIALOGUE_DB = 18.0  # BGM 整体响度比对白低多少 LU：初值，待在线样本调参
TAIL_WINDOW_S = 1.0  # 末段窗口：1 秒（2 秒窗口会把一个 2 秒淡出平均成只低 4 dB，看不出收尾）


class MusicError(Exception):
    pass


# ---- 情绪映射：scene.mood（schema 枚举）→ 中文描述 ----

MOOD_TABLE: dict[str, dict[str, str]] = {
    "tense": {"style": "紧张悬疑的电影配乐，低频脉冲推进", "emotion": "紧张、压迫、不安", "instruments": "低音提琴、弦乐颤音、打击乐脉冲", "tempo": "中速偏快，约 100 BPM"},
    "warm": {"style": "温暖治愈的轻音乐", "emotion": "温暖、柔和、安心", "instruments": "原声吉他、钢琴、轻柔弦乐", "tempo": "中慢速，约 80 BPM"},
    "sad": {"style": "忧伤克制的电影配乐", "emotion": "悲伤、压抑、克制", "instruments": "钢琴、大提琴、弦乐铺底", "tempo": "慢速，约 60 BPM"},
    "romantic": {"style": "浪漫抒情的轻音乐", "emotion": "浪漫、甜蜜、柔情", "instruments": "钢琴、小提琴、竖琴", "tempo": "中慢速，约 75 BPM"},
    "suspense": {"style": "悬疑氛围配乐", "emotion": "悬疑、神秘、不安", "instruments": "合成器氛围垫、钢琴单音、低频弦乐", "tempo": "慢速，约 70 BPM"},
    "comic": {"style": "轻快诙谐的配乐", "emotion": "轻松、幽默、俏皮", "instruments": "木管、拨弦、木琴", "tempo": "中快速，约 110 BPM"},
    "uplifting": {"style": "积极向上的励志配乐", "emotion": "振奋、希望、热血", "instruments": "钢琴、弦乐、鼓组", "tempo": "中快速，约 120 BPM"},
    "neutral": {"style": "中性的背景音乐", "emotion": "平静、克制、不抢戏", "instruments": "钢琴、轻柔的电子音色垫", "tempo": "中慢速，约 85 BPM"},
}
TIME_OF_DAY_CN = {"day": "白天", "night": "夜晚", "dawn": "清晨", "dusk": "黄昏"}
INT_EXT_CN = {"INT": "室内", "EXT": "室外"}


def schema_enum(*path: str) -> list[str]:
    """从 DramaIR schema 读枚举（单测用它校验映射表覆盖全部枚举，防漂移）。"""
    node: Any = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for key in path:
        node = node[key]
    return list(node["enum"])


def load_template(version: str = PROMPT_VERSION):
    try:
        return costume.load_templates(version)["bgm"]
    except (OSError, KeyError) as exc:
        raise MusicError(f"找不到提示词模板 {version}：{exc}") from None


def render_prompt(doc: Mapping[str, Any], scene: Mapping[str, Any], version: str = PROMPT_VERSION) -> str:
    mood = scene["mood"]
    if mood not in MOOD_TABLE:
        raise MusicError(f"{scene['scene_id']}：mood={mood!r} 没有映射（MOOD_TABLE 需要与 schema 枚举同步）")
    setting = scene["setting"]
    text = load_template(version).substitute(
        title=doc["series"]["title"], genre=doc["series"]["genre"],
        time_of_day=TIME_OF_DAY_CN[setting["time_of_day"]], int_ext=INT_EXT_CN[setting["int_ext"]],
        location=setting["location"], setting_desc=setting["description"], **MOOD_TABLE[mood],
    )
    return " ".join(text.split())


# ---- 时间轴 ----


@dataclass
class Span:
    scene_id: str
    start_s: float
    end_s: float

    @property
    def dur_s(self) -> float:
        return round(self.end_s - self.start_s, 4)


def _scenes(doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(doc["episodes"][0]["scenes"])


def spans_from_hints(doc: Mapping[str, Any]) -> list[Span]:
    """hint 口径：按 shot.duration.hint_s 累加（样例合计 60 秒，不含台词撑出的时长）。"""
    out, cursor = [], 0.0
    for scene in _scenes(doc):
        dur = sum(float(s["duration"]["hint_s"]) for s in scene["shots"])
        out.append(Span(scene["scene_id"], round(cursor, 4), round(cursor + dur, 4)))
        cursor += dur
    return out


def spans_from_compose(doc: Mapping[str, Any], plan: compose.Plan) -> list[Span]:
    """成片口径：取 compose.plan 里每个镜头的起点与目标时长（含台词撑出的时长）。"""
    by_shot = {s.shot_id: s for s in plan.shots}
    out = []
    for scene in _scenes(doc):
        try:
            shots = [by_shot[s["shot_id"]] for s in scene["shots"]]
        except KeyError as exc:
            raise MusicError(f"成片规划里没有镜头 {exc}") from None
        start = shots[0].start_frames / compose.FPS
        end = (shots[-1].start_frames + shots[-1].target_frames) / compose.FPS
        out.append(Span(scene["scene_id"], round(start, 4), round(end, 4)))
    return out


# ---- BGM 规划 ----


@dataclass
class BgmSegment:
    scene_id: str
    mood: str
    start_s: float  # 在成片时间线上的起点（含交叉淡化，首段为 0）
    end_s: float
    play_s: float
    gen_s: int  # 请求的生成时长（≥ 30 秒）
    trim_s: float  # 合成时裁掉的部分 = gen_s - play_s
    fade_in_s: float
    fade_out_s: float
    text: str
    prompt_version: str
    node_key: str
    est_cny: float


def node_key(text: str, gen_s: int, version: str = PROMPT_VERSION) -> str:
    """幂等键（INV-05 的雏形）：请求内容的 sha256。场景号 / 起止不参与——同样的请求同样的产物。"""
    payload = {"action": volc_sign.ACTION_SUBMIT, "model": MODEL_VERSION, "prompt_version": version, "text": text, "duration": gen_s}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def plan_bgm(doc: Mapping[str, Any], spans: list[Span], xfade_s: float = XFADE_S, version: str = PROMPT_VERSION) -> list[BgmSegment]:
    scenes = {s["scene_id"]: s for s in _scenes(doc)}
    out: list[BgmSegment] = []
    for i, span in enumerate(spans):
        first, last = i == 0, i == len(spans) - 1
        start = span.start_s if first else span.start_s - xfade_s / 2
        end = span.end_s if last else span.end_s + xfade_s / 2
        play = round(end - start, 4)
        gen = max(MIN_GEN_S, math.ceil(play - 1e-9))
        if gen > MAX_GEN_S:
            raise MusicError(f"{span.scene_id}：需要 {play:.1f} 秒，超过单次生成上限 {MAX_GEN_S} 秒（需要拆段，本步不做）")
        scene = scenes[span.scene_id]
        text = render_prompt(doc, scene, version)
        out.append(BgmSegment(
            span.scene_id, scene["mood"], round(start, 4), round(end, 4), play, gen, round(gen - play, 4),
            0.0 if first else xfade_s, TAIL_FADE_S if last else xfade_s, text, version, node_key(text, gen, version),
            pricing.music_cny(volc_sign.PROVIDER, volc_sign.ACTION_SUBMIT, gen),
        ))
    return out


def check_coverage(segments: list[BgmSegment], total_s: float, tol: float = 1e-3) -> list[str]:
    """各段是否覆盖 [0, total_s] 且相邻段无空洞；返回问题列表（空 = 通过）。"""
    problems = []
    if not segments:
        return ["没有 BGM 段"]
    if abs(segments[0].start_s) > tol:
        problems.append(f"首段起点 {segments[0].start_s} ≠ 0")
    if abs(segments[-1].end_s - total_s) > tol:
        problems.append(f"末段终点 {segments[-1].end_s} ≠ 成片 {total_s}")
    for a, b in zip(segments, segments[1:]):
        if b.start_s > a.end_s + tol:
            problems.append(f"{a.scene_id} → {b.scene_id}：空洞 {b.start_s - a.end_s:.3f} 秒")
    return problems


# ---- 音效规划 ----


@dataclass(frozen=True)
class SfxRule:
    keywords: tuple[str, ...]
    category: str
    label: str
    loop: bool  # 是否整镜循环铺底
    gain_db: float  # 相对对白的默认增益（初值）
    start_offset_s: float  # 点音效：相对镜头起点
    dur_s: float | None  # 点音效默认时长；None = 整镜
    note: str = ""


# 先匹配先赢，顺序有意义（如“脚步踩水声”含“水”，“雨打伞面声”含“雨”）
SFX_RULES: tuple[SfxRule, ...] = (
    SfxRule(("议论",), "crowd_cut", "人群声（带截止点）", False, -22.0, 0.0, 1.5, "“戛然而止”：截止点应对齐动作 / 对白起点，规则只给默认时长，需人工定"),
    SfxRule(("脚步",), "foley_loop", "脚步拟音（整镜）", True, -20.0, 0.0, None, "随镜头内人物走动，起止需对齐画面"),
    SfxRule(("胶带", "键盘", "推门", "旋转门", "门"), "foley", "拟音（点音效）", False, -18.0, 0.2, 1.5),
    SfxRule(("提示音", "震动"), "ui", "提示音 / 震动（点音效）", False, -20.0, 0.2, 1.0, "手机 / 电脑提示通常出现在动作之后，起点待对齐画面"),
    SfxRule(("雨",), "ambience", "环境铺底（雨）", True, -26.0, 0.0, None, "整镜循环；“渐大”等变化用增益包络，规则只给整镜"),
)
UNCATEGORIZED = SfxRule((), "uncategorized", "未归类", False, -22.0, 0.0, None, "没有命中规则，需要人工归类")
SFX_SOURCES = ("音效库检索（未选库）", "文本生成（如 Seed Audio，未评测）", "H3 视频原声（待评估，D-014 选 off）")


def classify_sfx(tag: str) -> SfxRule:
    for rule in SFX_RULES:
        if any(k in tag for k in rule.keywords):
            return rule
    return UNCATEGORIZED


@dataclass
class SfxItem:
    shot_id: str
    tag: str
    category: str
    label: str
    start_s: float
    end_s: float
    loop: bool
    gain_db: float
    note: str
    sources: tuple[str, ...] = SFX_SOURCES
    status: str = "unsourced"


def sfx_plan(doc: Mapping[str, Any], shot_spans: Mapping[str, tuple[float, float]]) -> list[SfxItem]:
    """shot_spans：shot_id → (起点, 终点)（成片口径或 hint 口径）。每个 sfx 标签恰好出现一次。"""
    out: list[SfxItem] = []
    for scene in _scenes(doc):
        for shot in scene["shots"]:
            start, end = shot_spans[shot["shot_id"]]
            for tag in shot.get("sfx", []):
                rule = classify_sfx(tag)
                if rule.dur_s is None:
                    s, e = start, end
                else:
                    s = start + rule.start_offset_s
                    e = min(end, s + rule.dur_s)
                out.append(SfxItem(shot["shot_id"], tag, rule.category, rule.label, round(s, 4), round(e, 4), rule.loop, rule.gain_db, rule.note))
    return out


def shot_spans_from_hints(doc: Mapping[str, Any]) -> dict[str, tuple[float, float]]:
    out, cursor = {}, 0.0
    for scene in _scenes(doc):
        for shot in scene["shots"]:
            dur = float(shot["duration"]["hint_s"])
            out[shot["shot_id"]] = (round(cursor, 4), round(cursor + dur, 4))
            cursor += dur
    return out


def shot_spans_from_compose(plan: compose.Plan) -> dict[str, tuple[float, float]]:
    return {s.shot_id: (round(s.start_frames / compose.FPS, 4), round((s.start_frames + s.target_frames) / compose.FPS, 4)) for s in plan.shots}


# ---- 度量 ----


@dataclass
class AudioReport:
    file: str
    duration_s: float | None
    expected_s: float | None
    duration_delta_s: float | None
    codec: str
    sample_rate: int
    channels: int
    lufs: float | None
    true_peak_dbtp: float | None
    lra_lu: float | None
    leading_silence_s: float
    trailing_silence_s: float
    tail_mean_db: float | None  # 最后 TAIL_WINDOW_S 秒的平均音量（volumedetect）
    tail_drop_db: float | None  # 末段平均音量 − 全段平均音量
    ending: str  # natural / needs_fade / unknown
    suggested_gain_db: float | None = None
    suggestion_note: str = ""


def analyze_file(path: Path, expected_s: float | None = None, reference_lufs: float | None = None, below_db: float = BELOW_DIALOGUE_DB) -> AudioReport:
    """单个音频文件的度量。没有音频流或文件不存在抛 MediaError。"""
    info = media.probe_audio(path)
    if info is None:
        raise media.MediaError(f"{path}：没有音频流")
    duration = info.duration_s or media.format_duration(path)
    loud = media.loudness(path)
    sil = media.silences(path)
    leading = next((e or (duration or 0.0) for s, e in sil if s <= 0.05), 0.0)
    trailing = 0.0
    if duration is not None:
        for s, e in sil:
            if e is None or e >= duration - 0.05:
                trailing = round(duration - s, 3)
    tail = whole = None
    if duration is not None and duration > TAIL_WINDOW_S + 0.5:
        tail = media.mean_volume_db(path, start_s=duration - TAIL_WINDOW_S, dur_s=TAIL_WINDOW_S)
        whole = media.mean_volume_db(path)
    drop = round(tail - whole, 2) if tail is not None and whole is not None else None
    if drop is None:
        ending = "unknown"
    else:
        ending = "natural" if drop <= -6.0 or trailing >= 0.3 else "needs_fade"
    rep = AudioReport(
        file=str(path), duration_s=round(duration, 3) if duration is not None else None, expected_s=expected_s,
        duration_delta_s=round(duration - expected_s, 3) if duration is not None and expected_s is not None else None,
        codec=info.codec, sample_rate=info.sample_rate, channels=info.channels, lufs=loud.integrated_lufs, true_peak_dbtp=loud.true_peak_dbtp,
        lra_lu=loud.loudness_range_lu, leading_silence_s=round(leading, 3), trailing_silence_s=trailing, tail_mean_db=tail, tail_drop_db=drop, ending=ending,
    )
    if reference_lufs is not None and loud.integrated_lufs is not None:
        rep.suggested_gain_db = round(reference_lufs - below_db - loud.integrated_lufs, 1)
        rep.suggestion_note = f"使整体响度比对白轨（{reference_lufs:.1f} LUFS）低 {below_db:g} LU；初值，待在线样本调参"
    return rep


def analyze_paths(paths: list[Path], segments: list[BgmSegment], reference: Path | None, below_db: float = BELOW_DIALOGUE_DB) -> list[AudioReport]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(f for f in p.iterdir() if f.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".mp4", ".ogg"}))
        elif p.is_file():
            files.append(p)
        else:
            raise media.MediaError(f"文件不存在：{p}")
    if not files:
        raise media.MediaError("没有可分析的音频文件")
    ref_lufs = media.loudness(reference).integrated_lufs if reference is not None and reference.is_file() else None
    expected = {s.scene_id: float(s.gen_s) for s in segments}
    return [analyze_file(f, expected.get(f.stem), ref_lufs, below_db) for f in files]


# ---- 输出 ----


def build_plan(args_ir: Path, basis: str, compose_kwargs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    doc, sha = tts.load_episode(args_ir)
    if basis == "hint":
        spans, shot_spans, total = spans_from_hints(doc), shot_spans_from_hints(doc), None
        total = spans[-1].end_s
    else:
        cp = compose.plan(args_ir, **(compose_kwargs or {}))
        spans, shot_spans, total = spans_from_compose(doc, cp), shot_spans_from_compose(cp), cp.total_s
    segments = plan_bgm(doc, spans)
    items = sfx_plan(doc, shot_spans)
    problems = check_coverage(segments, total)
    return {
        "ir_sha256": sha, "basis": basis, "total_s": total, "prompt_version": PROMPT_VERSION, "model_version": MODEL_VERSION,
        "segments": [asdict(s) for s in segments], "sfx": [asdict(i) | {"sources": list(i.sources)} for i in items],
        "coverage_problems": problems,
        "estimate": {
            "gen_s_total": sum(s.gen_s for s in segments), "cost_cny": round(sum(s.est_cny for s in segments), 6),
            "unit_cny_per_s": pricing.MUSIC_CNY_PER_SECOND[(volc_sign.PROVIDER, volc_sign.ACTION_SUBMIT)],
            "verified": pricing.MUSIC_PRICE_VERIFIED, "source": pricing.MUSIC_PRICE_SOURCE,
        },
    }


def render_text(plan: Mapping[str, Any]) -> str:
    est = plan["estimate"]
    lines = [f"BGM 规划（{plan['basis']} 口径，成片 {plan['total_s']:g} 秒，模板 {plan['prompt_version']}，模型 {plan['model_version']}）", ""]
    lines.append("场景\t情绪\t起\t止\t播放\t生成\t裁掉\t淡入\t淡出\t预估¥")
    for s in plan["segments"]:
        lines.append(f"{s['scene_id']}\t{s['mood']}\t{s['start_s']:.2f}\t{s['end_s']:.2f}\t{s['play_s']:.2f}\t{s['gen_s']}\t{s['trim_s']:.2f}\t{s['fade_in_s']:g}\t{s['fade_out_s']:g}\t{s['est_cny']:.3f}")
    lines += ["", "提示词："]
    lines += [f"- {s['scene_id']}（{s['node_key'][:12]}）：{s['text']}" for s in plan["segments"]]
    flag = "已核对" if est["verified"] else "未核对账单"
    lines += ["", f"预估费用：生成 {est['gen_s_total']} 秒 × ¥{est['unit_cny_per_s']:g} / 秒 ≈ ¥{est['cost_cny']:.3f}（单价{flag}；{est['source']}）"]
    lines += ["覆盖检查：" + ("通过（各段覆盖成片全长，段间无空洞）" if not plan["coverage_problems"] else "；".join(plan["coverage_problems"])), ""]
    lines.append("音效规划（status=unsourced：没有选定来源，规则给出的起止与增益只是初值）")
    lines.append("镜头\t标签\t类别\t起\t止\t循环\t增益dB")
    for i in plan["sfx"]:
        lines.append(f"{i['shot_id']}\t{i['tag']}\t{i['category']}\t{i['start_s']:.2f}\t{i['end_s']:.2f}\t{'是' if i['loop'] else '否'}\t{i['gain_db']:g}")
    unk = [i["tag"] for i in plan["sfx"] if i["category"] == "uncategorized"]
    lines.append(f"共 {len(plan['sfx'])} 条标签；未归类 {len(unk)} 条" + (f"：{'、'.join(unk)}" if unk else ""))
    return "\n".join(lines)


def render_report(reports: list[AudioReport]) -> str:
    lines = ["文件\t时长\t期望\t偏差\tLUFS\t真峰值\tLRA\t首静音\t尾静音\t末1秒均值dB\t落差\t收尾\t建议增益dB"]
    for r in reports:
        f = lambda v, p=2: "—" if v is None else f"{v:.{p}f}"  # noqa: E731
        lines.append(f"{Path(r.file).name}\t{f(r.duration_s)}\t{f(r.expected_s, 0)}\t{f(r.duration_delta_s)}\t{f(r.lufs, 1)}\t{f(r.true_peak_dbtp, 1)}\t{f(r.lra_lu, 1)}\t"
                     f"{r.leading_silence_s:.2f}\t{r.trailing_silence_s:.2f}\t{f(r.tail_mean_db, 1)}\t{f(r.tail_drop_db, 1)}\t{r.ending}\t{f(r.suggested_gain_db, 1)}")
    return "\n".join(lines)


# ---- 在线生成（有密钥时） ----


class CostLimit(Exception):
    pass


@dataclass
class GenResult:
    scene_id: str
    node_key: str
    task_id: str | None
    file: str | None
    sha256: str | None
    requested_s: int
    billed_s: float | None
    cost_cny: float
    ok: bool
    error: str | None = None
    attempts: int = 1


def _suffix(codec: str) -> str:
    return {"pcm_s16le": ".wav", "pcm_s24le": ".wav", "mp3": ".mp3", "aac": ".m4a", "flac": ".flac"}.get(codec, ".bin")


def _retry_same_task(fn: Callable[[], Any], retries: int, sleep: Callable[[float], None]) -> Any:
    """对已提交的任务重试取结果：只重试 transient 的 VolcError（task_failed 不在此重试，由外层决定是否重新提交）。"""
    for attempt in range(1, retries + 2):
        try:
            return fn()
        except volc_sign.VolcError as exc:
            if exc.kind == "task_failed" or not exc.transient or attempt > retries:
                raise
            sleep(2.0 ** attempt)
    raise AssertionError("unreachable")


def generate(
    segments: list[BgmSegment],
    out_dir: Path,
    env: Mapping[str, str],
    *,
    max_cost_cny: float,
    base_dir: Path | None = None,
    retries: int = 2,
    transport: volc_sign.Transport | None = None,
    clock: volc_sign.Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
    probe: Callable[[Path], Any] = media.probe_audio,
    only: str | None = None,
    out: TextIO | None = None,
) -> list[GenResult]:
    """逐段生成。费用保险按请求时长预估（后付费按成功时长，实际 ≤ 请求）；失败的任务不计费（文档：按最终成功生成时长）。"""
    out = sys.stdout if out is None else out
    creds = volc_sign.Credentials.from_env(env)
    todo = [s for s in segments if only in (None, s.scene_id)]
    if not todo:
        raise MusicError(f"--only {only}：没有这个场景")
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[GenResult] = []
    spent = 0.0
    args = {"out_dir": str(out_dir), "max_cost_cny": max_cost_cny, "segments": [s.scene_id for s in todo], "prompt_version": PROMPT_VERSION}
    with Run("music", args, base_dir=base_dir, secrets=creds.secrets) as run:
        print(f"run_id: {run.run_id}", file=out)
        for seg in todo:
            if spent + seg.est_cny > max_cost_cny + 1e-9:
                raise CostLimit(f"累计 ¥{spent:.3f} + 本段 ¥{seg.est_cny:.3f} 将超过上限 ¥{max_cost_cny:g}")
            res = GenResult(seg.scene_id, seg.node_key, None, None, None, seg.gen_s, None, 0.0, False)
            body = volc_sign.build_submit_body(seg.text, seg.gen_s)
            submitted_unresolved = 0.0  # 已提交但没拿到结果的任务：可能已计费，计入费用保险
            for attempt in range(1, retries + 2):  # 外层只在“提交失败”或“任务失败且码可重试”时重新提交
                res.attempts = attempt
                try:
                    with run.call(volc_sign.PROVIDER, "music_sfx", model=f"{volc_sign.ACTION_SUBMIT}/{MODEL_VERSION}", node_key=seg.node_key) as call:
                        call.extra = {"scene_id": seg.scene_id, "attempt": attempt, "op": "submit"}
                        sub = volc_sign.submit_bgm(body, env=env, transport=transport, clock=clock)
                        call.request_id = sub.task_id
                        call.cost_cny, call.cost_basis = 0.0, "free"  # 提交本身不计费，成功后按时长记账
                    res.task_id = sub.task_id
                    submitted_unresolved = seg.est_cny
                    # 轮询与下载在原 task_id 上就地重试，不重新提交（重新提交 = 再付一次费）
                    st = _retry_same_task(lambda: volc_sign.poll_song(sub.task_id, env=env, transport=transport, clock=clock, sleep=sleep), retries, sleep)
                    audio = _retry_same_task(lambda: volc_sign.download(st.audio_url or "", transport=transport), retries, sleep)
                    billed = st.duration_s if st.duration_s is not None else float(seg.gen_s)
                    cost = pricing.music_cny(volc_sign.PROVIDER, volc_sign.ACTION_SUBMIT, billed)
                    with run.call(volc_sign.PROVIDER, "music_sfx", model=f"{volc_sign.ACTION_QUERY}/{MODEL_VERSION}", node_key=seg.node_key) as call:
                        call.extra = {"scene_id": seg.scene_id, "op": "result", "task_id": sub.task_id, "raw": st.raw}
                        call.cost_cny, call.cost_basis = cost, "estimate"
                        call.usage = {"duration_s": billed, "bytes": len(audio)}
                    submitted_unresolved = 0.0
                    spent += cost  # 任务已成功并计费，先记账再落盘（落盘失败不会丢账）
                    res.billed_s, res.cost_cny = billed, cost
                    tmp = out_dir / f".{seg.scene_id}.part"
                    tmp.write_bytes(audio)
                    info = probe(tmp)
                    dest = out_dir / f"{seg.scene_id}{_suffix(info.codec) if info else '.bin'}"
                    tmp.replace(dest)
                    res.file, res.sha256, res.ok, res.error = dest.name, hashlib.sha256(audio).hexdigest(), True, None
                    break
                except (volc_sign.VolcError, media.MediaError, OSError) as exc:
                    if getattr(exc, "kind", "") == "task_failed":
                        submitted_unresolved = 0.0  # 失败的任务不计费（按最终成功生成的时长计费）
                    res.error = str(exc) + (f" (code={exc.api_code})" if getattr(exc, "api_code", None) is not None else "")
                    if res.billed_s is not None:  # 已计费但落盘失败：不再重新提交
                        break
                    transient = getattr(exc, "transient", False)
                    if getattr(exc, "kind", "") != "task_failed" and res.task_id and submitted_unresolved:
                        break  # 提交过且轮询 / 下载重试耗尽：不重新提交
                    if not transient or attempt > retries:
                        break
                    sleep(2.0 ** attempt)
            spent += submitted_unresolved
            res.cost_cny = res.cost_cny or submitted_unresolved
            if submitted_unresolved:
                res.error = f"{res.error}；任务 {res.task_id} 已提交但没有取回结果，可能已计费（按请求时长 ¥{submitted_unresolved:.3f} 计入费用保险），可用 QuerySong 查询"
            results.append(res)
            print(f"{seg.scene_id}: {'ok' if res.ok else '失败'} {res.file or res.error}", file=out)
        manifest = {"prompt_version": PROMPT_VERSION, "model_version": MODEL_VERSION, "results": [asdict(r) for r in results], "cost_cny": round(spent, 6), "cost_basis": "estimate"}
        run.write_json("summary.json", manifest)
        (out_dir / "bgm-manifest.json").write_text(config.redact(json.dumps(manifest, ensure_ascii=False, indent=2), creds.secrets) + "\n", encoding="utf-8")
    return results


# ---- CLI ----


def _cmd(args: argparse.Namespace, out: TextIO | None = None, env: Mapping[str, str] | None = None) -> int:
    out = sys.stdout if out is None else out
    env = os.environ if env is None else env
    ir = Path(args.ir)
    modes = [m for m, v in (("--dry-run", args.dry_run), ("--analyze", args.analyze), ("--export", args.export)) if v]
    if len(modes) > 1:
        args.parser.error(f"{' 与 '.join(modes)} 不能同时使用")
    if args.analyze:
        try:
            doc, _ = tts.load_episode(ir)
            segments = plan_bgm(doc, spans_from_hints(doc)) if args.basis == "hint" else plan_bgm(doc, spans_from_compose(doc, compose.plan(ir)))
            reports = analyze_paths([Path(p) for p in args.analyze], segments, Path(args.reference) if args.reference else None, args.below_db)
        except (media.MediaError, MusicError, compose.ComposeError) as exc:
            print(f"分析失败：{exc}", file=out)
            return 1
        print(json.dumps([asdict(r) for r in reports], ensure_ascii=False, indent=2) if args.json else render_report(reports), file=out)
        return 0
    try:
        plan = build_plan(ir, args.basis)
    except (MusicError, compose.ComposeError) as exc:
        print(f"规划失败：{exc}", file=out)
        return 1
    if args.dry_run or args.export:
        if args.export:
            d = Path(args.export)
            d.mkdir(parents=True, exist_ok=True)
            (d / "music-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"已写 {d / 'music-plan.json'}", file=out)
        print(json.dumps(plan, ensure_ascii=False, indent=2) if args.json else render_text(plan), file=out)
        return 1 if plan["coverage_problems"] else 0
    missing = [v for v in (volc_sign.AK_ENV, volc_sign.SK_ENV) if not (env.get(v) or "").strip()]
    if missing:
        print(f"缺少 {'、'.join(missing)}：在云环境设置或 spikes/poc/.env 中配置（IAM 访问密钥，建议子账号；见 README）。只想看规划请加 --dry-run", file=out)
        return 2
    doc_segments = [BgmSegment(**s) for s in plan["segments"]]
    try:
        results = generate(doc_segments, Path(args.out), env, max_cost_cny=args.max_cost_cny, only=args.only, out=out)
    except (CostLimit, MusicError, volc_sign.VolcError) as exc:
        print(f"中止：{exc}", file=out)
        return 1
    return 0 if all(r.ok for r in results) else 1


def add_parser(sub) -> None:
    p = sub.add_parser("music", help="BGM 与音效：按场景情绪规划、度量、（有密钥时）生成（P0-10，豆包音乐）")
    p.add_argument("--ir", default=str(script.SAMPLE_EP01))
    p.add_argument("--basis", choices=("compose", "hint"), default="compose", help="时间轴口径：compose = 成片（含台词撑出的时长，默认）；hint = shot.duration.hint_s 累加")
    p.add_argument("--dry-run", action="store_true", help="只规划并打印 BGM 段、音效表与预估费用：不要密钥、不联网、不写 runs/")
    p.add_argument("--export", metavar="目录", help="离线：把规划写成 <目录>/music-plan.json 并打印")
    p.add_argument("--json", action="store_true", help="与 --dry-run / --export / --analyze 一起用：输出 JSON")
    p.add_argument("--analyze", nargs="+", metavar="音频或目录", help="离线：度量音频文件（时长、LUFS、真峰值、LRA、首尾静音、末 1 秒电平落差、混音增益建议）")
    p.add_argument("--reference", default=str(DEFAULT_REFERENCE), help="--analyze 的对白轨参考（默认 P0-11 的 ambient-off.m4a）")
    p.add_argument("--below-db", type=float, default=BELOW_DIALOGUE_DB, help="--analyze：BGM 整体响度比对白低多少 LU（初值，待在线调参）")
    p.add_argument("--out", default=str(config.PROJECT_DIR / "runs" / "music-out"), help="生成模式的输出目录")
    p.add_argument("--only", metavar="场景", help="生成模式：只生成这个场景")
    p.add_argument("--max-cost-cny", type=float, default=2.0, help="生成模式累计估算费用上限（元）")
    p.set_defaults(func=_cmd, parser=p)
