"""P0-11 渲染：读 OTIO 转成的 spec（otio_timeline.to_spec），用 FFmpeg 渲染成片。只用标准库 + 系统 ffmpeg，不联网，不调模型。

三类节点，各有 node_key（输入内容 + 全部渲染参数 + 字体内容 + ffmpeg 版本的 sha256），按 node_key 缓存到 cache_dir：
- segment：一个镜头的 1080×1920、24 fps 画面段（真实片段：缩放到高 1920 再居中裁到 1080，不足的帧冻结尾帧；占位：首帧缓慢推近 + “占位·静帧”标签）；
- audio_mix：整条音轨（对白逐句放置 + 可选的 H3 原声，48 kHz 立体声，两遍 loudnorm 到 −16 LUFS）；
- final：拼接 + 字幕 + AIGC 角标 + 混流 + 容器元数据。
补入一个真实片段时，只有该镜头的 segment、（原声档位非 off 时的）audio_mix 与 final 的 node_key 变化，其余镜头命中缓存。

滤镜里不出现任何用户可控的路径或文本：字体、字幕、标签文字先复制 / 写进缓存目录里的固定文件名（font.ttc、sub-<哈希>.ass、label.txt、badge.txt），
ffmpeg 以缓存目录为工作目录、滤镜只引用这些相对文件名，避免路径里的 `; [ ] ' %` 等字符破坏滤镜串。
AIGC 角标文字取常量 BADGE_TEXT，不读 OTIO 里的文字（OTIO 被改动不能让角标消失），并要求角标 clip 覆盖全片。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from poc import media, subtitles

RENDER_VERSION = "2"
BADGE_TEXT = "AI生成"
AMBIENT_GAIN_DB = {"off": None, "low": -24.0, "duck": -12.0}  # H3 原声的基础增益；duck 档再被对白侧链压低
LUFS_TARGET = -16.0
TRUE_PEAK_TARGET = -1.5
SAMPLE_RATE = 48000
SEGMENT_CRF = 18
FINAL_CRF = 23
FINAL_MAXRATE = "2500k"
FINAL_BUFSIZE = "5000k"
PLACEHOLDER_LABEL = "占位·静帧"
# 影响输出字节的全部常量：进每个节点的 key（改任何一个，缓存自动失效，不靠手工改 RENDER_VERSION）
RENDER_PARAMS = {
    "segment": {"crf": SEGMENT_CRF, "preset": "medium", "zoom_scale": 2, "label_size": 40, "label_box": "black@0.5:12", "label_pos": "48:h-th-120"},
    "audio": {"sr": SAMPLE_RATE, "lufs": LUFS_TARGET, "tp": TRUE_PEAK_TARGET, "lra": 11, "duck": "threshold=0.02:ratio=10:attack=20:release=400", "pcm": "s16le"},
    "final": {"crf": FINAL_CRF, "maxrate": FINAL_MAXRATE, "bufsize": FINAL_BUFSIZE, "preset": "medium", "aac": "160k", "badge_size": 46,
              "badge_box": "black@0.45:14", "badge_pos": "w-tw-48:72", "sub_font": subtitles.FONT_NAME},
}


class RenderError(Exception):
    """渲染输入与规划不一致（素材被替换、角标缺失等）。"""


def _key(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_sha(path: str, expected: str | None, what: str) -> None:
    """渲染前重新哈希素材：规划之后被替换、或 OTIO 被手改，都不能让缓存按旧哈希命中。"""
    if not Path(path).is_file():
        raise RenderError(f"{what}：素材不存在 {path}")
    if expected and _sha(Path(path)) != expected:
        raise RenderError(f"{what}：素材 sha256 与时间线记录不一致（素材被替换？）{path}")


@dataclass
class Node:
    kind: str
    name: str
    key: str
    hit: bool
    path: str
    seconds: float = 0.0


@dataclass
class Result:
    nodes: list[Node] = field(default_factory=list)
    final: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"nodes": [n.__dict__ for n in self.nodes], "hits": sum(n.hit for n in self.nodes), "misses": sum(not n.hit for n in self.nodes)}


class Cache:
    """缓存目录，同时是 ffmpeg 的工作目录：字体、标签文字用固定文件名放在这里，滤镜只引用它们。"""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        font = Path(subtitles.FONT_FILE)
        self.font_sha = _sha(font)
        dst = root / "font.ttc"
        if not dst.exists() or dst.stat().st_size != font.stat().st_size:
            shutil.copyfile(font, dst)
        (root / "label.txt").write_text(PLACEHOLDER_LABEL, encoding="utf-8")
        (root / "badge.txt").write_text(BADGE_TEXT, encoding="utf-8")
        self.env = {"v": RENDER_VERSION, "params": RENDER_PARAMS, "font": self.font_sha, "ffmpeg": media.version()}

    def path(self, kind: str, key: str, ext: str) -> Path:
        return self.root / f"{kind}-{key[:24]}.{ext}"

    def tmp(self, final: Path) -> Path:
        return final.with_name(f"{final.stem}.{os.getpid()}.part{final.suffix}")  # 带 pid：并发运行互不覆盖，扩展名不变（ffmpeg 靠它选封装）

    def hit(self, path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0


def _cached(cache: Cache, dst: Path, build) -> bool:
    """dst 不存在就用 build(tmp) 生成：失败时清理临时文件；返回是否命中。"""
    if cache.hit(dst):
        return True
    tmp = cache.tmp(dst)
    try:
        build(tmp)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    return False


# ---- 画面段 ----


def segment_key(shot: dict[str, Any], size: tuple[int, int], fps: int, cache: Cache) -> str:
    return _key({"env": cache.env, "kind": "segment", "source": shot["source"], "sha256": shot["sha256"], "native": shot["native_frames"],
                 "freeze": shot["freeze_frames"], "target": shot["target_frames"], "zoom": shot["zoom"], "size": size, "fps": fps,
                 "label": PLACEHOLDER_LABEL if shot["placeholder"] else None})


def _segment_cmd(shot: dict[str, Any], dst: Path, size: tuple[int, int], fps: int) -> list[str]:
    w, h = size
    n = shot["target_frames"]
    base = [media.FFMPEG, "-nostdin", "-v", "error", "-y"]
    if shot["placeholder"]:
        zoom = shot["zoom"] or {"from": 1.0, "to": 1.08}
        z = f"{zoom['from']}+({zoom['to']}-{zoom['from']})*on/{max(1, n - 1)}"
        k = RENDER_PARAMS["segment"]["zoom_scale"]
        label = "drawtext=fontfile=font.ttc:textfile=label.txt:fontsize=40:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=12:x=48:y=h-th-120"
        vf = (f"scale={w * k}:{h * k}:flags=lanczos,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps},"
              f"{label},setsar=1,format=yuv420p")
        cmd = base + ["-framerate", str(fps), "-loop", "1", "-i", shot["media"], "-vf", vf, "-frames:v", str(n)]
    else:
        vf = f"scale=-2:{h}:flags=lanczos,crop={w}:{h},setsar=1,fps={fps}"
        if shot["freeze_frames"]:
            vf += f",tpad=stop_mode=clone:stop_duration={shot['freeze_frames'] / fps:.6f}"
        vf += ",format=yuv420p"
        cmd = base + ["-i", shot["media"], "-vf", vf, "-frames:v", str(n)]
    return cmd + ["-an", "-c:v", "libx264", "-preset", RENDER_PARAMS["segment"]["preset"], "-crf", str(SEGMENT_CRF), "-r", str(fps), "-movflags", "+faststart", str(dst)]


def render_segment(shot: dict[str, Any], cache: Cache, size: tuple[int, int], fps: int) -> Node:
    _check_sha(shot["media"], shot["sha256"], shot["shot_id"])
    key = segment_key(shot, size, fps, cache)
    dst = cache.path("segment", key, "mp4")
    t0 = time.monotonic()
    hit = _cached(cache, dst, lambda tmp: media._run(_segment_cmd(shot, tmp, size, fps), timeout=600, cwd=cache.root))
    return Node("segment", shot["shot_id"], key, hit, str(dst), 0.0 if hit else round(time.monotonic() - t0, 2))


# ---- 音频 ----


def audio_key(spec: dict[str, Any], ambient: str, cache: Cache) -> str:
    amb = [(c["sha256"], c["start_frames"], round(c["dur_s"], 4), AMBIENT_GAIN_DB[ambient]) for c in spec["ambient_clips"]] if ambient != "off" else []
    lines = [(ln["sha256"], round(ln["in_s"], 4), round(ln["dur_s"], 4), ln["start_frames"]) for ln in spec["lines"]]
    return _key({"env": cache.env, "kind": "audio", "ambient": ambient, "amb": amb, "lines": lines, "total": spec["total_frames"], "fps": spec["fps"]})


def _graph(spec: dict[str, Any], ambient: str, fps: int) -> tuple[list[str], str]:
    """返回 (输入文件参数, filter_complex 文本)；输出标签 [padded]。"""
    inputs: list[str] = []
    dlg: list[str] = []
    parts: list[str] = []
    idx = 0
    for ln in spec["lines"]:
        inputs += ["-i", ln["media"]]
        ms = round(ln["start_frames"] / fps * 1000)
        parts.append(f"[{idx}:a]atrim=start={ln['in_s']:.4f}:duration={ln['dur_s']:.4f},asetpts=PTS-STARTPTS,aresample={SAMPLE_RATE},"
                     f"pan=stereo|c0=c0|c1=c0,adelay={ms}|{ms}[d{idx}]")
        dlg.append(f"[d{idx}]")
        idx += 1
    parts.append("".join(dlg) + f"amix=inputs={len(dlg)}:normalize=0:duration=longest[dlgmix]")
    amb: list[str] = []
    if ambient != "off":
        for c in spec["ambient_clips"]:  # 原声 clip 在 OTIO 里总是存在（enabled 只反映成片所选档位），对比各档时按档位增益全部混入
            inputs += ["-i", c["media"]]
            ms = round(c["start_frames"] / fps * 1000)
            parts.append(f"[{idx}:a]atrim=duration={c['dur_s']:.4f},asetpts=PTS-STARTPTS,aresample={SAMPLE_RATE},"
                         f"aformat=channel_layouts=stereo,volume={AMBIENT_GAIN_DB[ambient]}dB,adelay={ms}|{ms}[a{idx}]")
            amb.append(f"[a{idx}]")
            idx += 1
    if amb:
        parts.append("".join(amb) + f"amix=inputs={len(amb)}:normalize=0:duration=longest[ambmix]")
        if ambient == "duck":
            parts.append("[dlgmix]asplit=2[dlg1][dlg2]")
            parts.append(f"[ambmix][dlg2]sidechaincompress={RENDER_PARAMS['audio']['duck']}[ambduck]")
            parts.append("[dlg1][ambduck]amix=inputs=2:normalize=0:duration=longest[premix]")
        else:
            parts.append("[dlgmix][ambmix]amix=inputs=2:normalize=0:duration=longest[premix]")
    else:
        parts.append("[dlgmix]anull[premix]")
    total_s = spec["total_frames"] / fps
    # apad 前必须 asetpts：amix 之后的时间戳不从 0 开始，atrim=duration 会把音频截成静音；总长由输出的 -t 控制
    parts.append(f"[premix]asetpts=PTS-STARTPTS,apad=whole_dur={total_s:.4f}[padded]")
    return inputs, ";".join(parts)


def _check_audio_inputs(spec: dict[str, Any], ambient: str) -> None:
    for ln in spec["lines"]:
        _check_sha(ln["media"], ln["sha256"], ln["line_id"])
    if ambient != "off":
        for c in spec["ambient_clips"]:
            _check_sha(c["media"], c["sha256"], c["shot_id"] + " 原声")


def render_audio(spec: dict[str, Any], ambient: str, cache: Cache, fps: int) -> Node:
    _check_audio_inputs(spec, ambient)
    key = audio_key(spec, ambient, cache)
    dst = cache.path("audio", key, "wav")
    t0 = time.monotonic()
    total = f"{spec['total_frames'] / fps:.4f}"

    def build(tmp: Path) -> None:
        inputs, graph = _graph(spec, ambient, fps)
        base = [media.FFMPEG, "-nostdin", "-v", "error", "-y", *inputs]
        p = RENDER_PARAMS["audio"]
        # 第 1 遍：测量（loudnorm 的 JSON 要 -v info 才打印）；第 2 遍：按测量值线性归一化
        measure = media._run([media.FFMPEG, "-nostdin", "-nostats", "-v", "info", "-y", *inputs, "-filter_complex",
                              graph + f";[padded]loudnorm=I={p['lufs']}:TP={p['tp']}:LRA={p['lra']}:print_format=json[mix]", "-map", "[mix]", "-f", "null", "-"], timeout=600)
        stats = json.loads(measure.stderr[measure.stderr.rindex("{"):])
        norm = (f"loudnorm=I={p['lufs']}:TP={p['tp']}:LRA={p['lra']}:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
                f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:linear=true")
        media._run(base + ["-filter_complex", graph + f";[padded]{norm},aresample={SAMPLE_RATE},asetpts=PTS-STARTPTS,apad=whole_dur={total}[mix]", "-map", "[mix]",
                           "-t", total, "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", f"pcm_{p['pcm']}", str(tmp)], timeout=600)

    hit = _cached(cache, dst, build)
    return Node("audio_mix", ambient, key, hit, str(dst), 0.0 if hit else round(time.monotonic() - t0, 2))


def premix_loudness(spec: dict[str, Any], ambient: str, cache: Cache, fps: int) -> float | None:
    """归一化之前的混音响度（LUFS）。"""
    inputs, graph = _graph(spec, ambient, fps)
    tmp = cache.root / f"premix-{ambient}.{os.getpid()}.wav"
    try:
        media._run([media.FFMPEG, "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[padded]", "-t", f"{spec['total_frames'] / fps:.4f}",
                    "-c:a", "pcm_s16le", str(tmp)], timeout=600)
        return media.loudness(tmp).integrated_lufs
    finally:
        tmp.unlink(missing_ok=True)


# ---- 终混 ----


def aigc_metadata(produce_id: str) -> str:
    """隐式标识（容器元数据 AIGC）。字段取自《人工智能生成合成内容标识办法》配套标准的元数据格式，字段与取值待对照 GB 45438-2025 确认。"""
    return json.dumps({"Label": "1", "ContentProducer": "Dramio-P0-spike", "ProduceID": produce_id, "ReservedCode1": "", "ContentPropagator": "",
                       "PropagateID": "", "ReservedCode2": ""}, ensure_ascii=False, separators=(",", ":"))


def final_key(segment_keys: list[str], audio: str, ass_sha: str, total_frames: int, cache: Cache) -> str:
    return _key({"env": cache.env, "kind": "final", "segments": segment_keys, "audio": audio, "ass": ass_sha, "badge": BADGE_TEXT, "total": total_frames,
                 "metadata": aigc_metadata("<ProduceID>")})


def _check_badge(spec: dict[str, Any]) -> None:
    badge = spec["badge"]
    if badge.get("text") != BADGE_TEXT or badge.get("frames") != spec["total_frames"]:
        raise RenderError(f"AIGC 角标缺失或被改动：时间线里的角标应为“{BADGE_TEXT}”并覆盖全片（{spec['total_frames']} 帧），实际 {badge.get('text')!r} / {badge.get('frames')}")


def render_final(spec: dict[str, Any], segments: list[Node], audio: Node, ass_path: Path, cache: Cache, out_path: Path, fps: int) -> Node:
    _check_badge(spec)
    ass_sha = _sha(ass_path)
    key = final_key([s.key for s in segments], audio.key, ass_sha, spec["total_frames"], cache)
    dst = cache.path("final", key, "mp4")
    produce_id = _key([s.key for s in segments] + [audio.key])[:32]
    t0 = time.monotonic()

    def build(tmp: Path) -> None:
        sub = cache.root / f"sub-{ass_sha[:12]}.ass"
        shutil.copyfile(ass_path, sub)
        listing = cache.root / f"concat-{key[:12]}.{os.getpid()}.txt"
        try:
            listing.write_text("".join(f"file '{Path(s.path).name}'\n" for s in segments), encoding="utf-8")  # 缓存文件名只有 [a-z0-9-.]
            p = RENDER_PARAMS["final"]
            vf = (f"subtitles={sub.name}:fontsdir=.,"
                  f"drawtext=fontfile=font.ttc:textfile=badge.txt:fontsize={p['badge_size']}:fontcolor=white:box=1:boxcolor=black@0.45:boxborderw=14:x=w-tw-48:y=72")
            media._run([
                media.FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", listing.name, "-i", audio.path,
                "-vf", vf, "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", p["preset"], "-crf", str(FINAL_CRF), "-maxrate", FINAL_MAXRATE,
                "-bufsize", FINAL_BUFSIZE, "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", p["aac"], "-ar", str(SAMPLE_RATE),
                "-t", f"{spec['total_frames'] / fps:.4f}", "-metadata", f"AIGC={aigc_metadata(produce_id)}",
                "-metadata", "comment=AI generated content (Dramio P0 spike); visible label burned in",
                "-movflags", "+faststart+use_metadata_tags", str(tmp),
            ], timeout=1800, cwd=cache.root)
        finally:
            listing.unlink(missing_ok=True)

    hit = _cached(cache, dst, build)
    shutil.copyfile(dst, out_path)
    return Node("final", "final", key, hit, str(dst), 0.0 if hit else round(time.monotonic() - t0, 2))


def render(spec: dict[str, Any], out_dir: Path, cache_dir: Path, ass_path: Path, ambient: str = "off", variants: tuple[str, ...] = ()) -> Result:
    """渲染成片到 out_dir/final.mp4；variants 里的其它原声档位只渲染音频（对比用）。"""
    if not media.available():
        raise media.MediaError("需要 ffmpeg / ffprobe")
    _check_badge(spec)
    size, fps = (int(spec["size"][0]), int(spec["size"][1])), int(spec["fps"])
    cache = Cache(cache_dir)
    result = Result()
    segs = [render_segment(s, cache, size, fps) for s in spec["shots"]]
    result.nodes += segs
    audio = render_audio(spec, ambient, cache, fps)
    result.nodes.append(audio)
    for mode in variants:
        if mode != ambient:
            result.nodes.append(render_audio(spec, mode, cache, fps))
    out_dir.mkdir(parents=True, exist_ok=True)
    final = render_final(spec, segs, audio, ass_path, cache, out_dir / "final.mp4", fps)
    result.nodes.append(final)
    result.final = out_dir / "final.mp4"
    return result
