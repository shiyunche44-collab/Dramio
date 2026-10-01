"""P0-11 渲染：读 OTIO 转成的 spec（otio_timeline.to_spec），用 FFmpeg 渲染成片。只用标准库 + 系统 ffmpeg，不联网，不调模型。

三类节点，各有 node_key（输入内容的 sha256），按 node_key 缓存到 cache_dir：
- segment：一个镜头的 1080×1920、24 fps 画面段（真实片段：缩放到高 1920 再居中裁到 1080，不足的帧冻结尾帧；占位：首帧缓慢推近 + “占位·静帧”标签）；
- audio_mix：整条音轨（对白逐句放置 + 可选的 H3 原声，48 kHz 立体声，两遍 loudnorm 到 −16 LUFS）；
- final：拼接 + 字幕 + AIGC 角标 + 混流 + 容器元数据。
补入一个真实片段时，只有该镜头的 segment、（原声档位非 off 时的）audio_mix 与 final 的 node_key 变化，其余镜头命中缓存。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from poc import media, subtitles

RENDER_VERSION = "1"
AMBIENT_GAIN_DB = {"off": None, "low": -24.0, "duck": -12.0}  # H3 原声的基础增益；duck 档再被对白侧链压低
LUFS_TARGET = -16.0
TRUE_PEAK_TARGET = -1.5
SAMPLE_RATE = 48000
SEGMENT_CRF = 18
FINAL_CRF = 23
FINAL_MAXRATE = "2500k"
FINAL_BUFSIZE = "5000k"
PLACEHOLDER_LABEL = "占位·静帧"
LABEL_FONT_SIZE = 40
BADGE_FONT_SIZE = 46


def _key(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _esc(text: str) -> str:
    """ffmpeg 滤镜参数转义（路径 / 文本里的 \\ : ' ,）。"""
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


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
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, kind: str, key: str, ext: str) -> Path:
        return self.root / f"{kind}-{key[:24]}.{ext}"


# ---- 画面段 ----


def segment_key(shot: dict[str, Any], size: tuple[int, int], fps: int) -> str:
    return _key({"v": RENDER_VERSION, "kind": "segment", "source": shot["source"], "sha256": shot["sha256"], "native": shot["native_frames"],
                 "freeze": shot["freeze_frames"], "target": shot["target_frames"], "zoom": shot["zoom"], "size": size, "fps": fps,
                 "crf": SEGMENT_CRF, "label": PLACEHOLDER_LABEL if shot["placeholder"] else None, "font": subtitles.FONT_FILE})


def _segment_cmd(shot: dict[str, Any], dst: Path, size: tuple[int, int], fps: int) -> list[str]:
    w, h = size
    n = shot["target_frames"]
    base = [media.FFMPEG, "-nostdin", "-v", "error", "-y"]
    if shot["placeholder"]:
        zoom = shot["zoom"] or {"from": 1.0, "to": 1.08}
        z = f"{zoom['from']}+({zoom['to']}-{zoom['from']})*on/{max(1, n - 1)}"
        label = (f"drawtext=fontfile={_esc(subtitles.FONT_FILE)}:text={_esc(PLACEHOLDER_LABEL)}:fontsize={LABEL_FONT_SIZE}:fontcolor=white:"
                 f"box=1:boxcolor=black@0.5:boxborderw=12:x=48:y=h-th-120")
        vf = (f"scale={w * 2}:{h * 2}:flags=lanczos,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps},"
              f"{label},setsar=1,format=yuv420p")
        cmd = base + ["-framerate", str(fps), "-loop", "1", "-i", shot["media"], "-vf", vf, "-frames:v", str(n)]
    else:
        vf = f"scale=-2:{h}:flags=lanczos,crop={w}:{h},setsar=1,fps={fps}"
        if shot["freeze_frames"]:
            vf += f",tpad=stop_mode=clone:stop_duration={shot['freeze_frames'] / fps:.6f}"
        vf += ",format=yuv420p"
        cmd = base + ["-i", shot["media"], "-vf", vf, "-frames:v", str(n)]
    return cmd + ["-an", "-c:v", "libx264", "-preset", "medium", "-crf", str(SEGMENT_CRF), "-r", str(fps), "-movflags", "+faststart", str(dst)]


def render_segment(shot: dict[str, Any], cache: Cache, size: tuple[int, int], fps: int) -> Node:
    key = segment_key(shot, size, fps)
    dst = cache.path("segment", key, "mp4")
    if dst.exists():
        return Node("segment", shot["shot_id"], key, True, str(dst))
    t0 = time.monotonic()
    tmp = dst.with_suffix(".part.mp4")
    media._run(_segment_cmd(shot, tmp, size, fps), timeout=600)
    tmp.replace(dst)
    return Node("segment", shot["shot_id"], key, False, str(dst), round(time.monotonic() - t0, 2))


# ---- 音频 ----


def audio_key(spec: dict[str, Any], ambient: str) -> str:
    amb = [(c["sha256"], c["start_frames"], round(c["dur_s"], 4), AMBIENT_GAIN_DB[ambient]) for c in spec["ambient_clips"]] if ambient != "off" else []
    lines = [(ln["sha256"], round(ln["in_s"], 4), round(ln["dur_s"], 4), ln["start_frames"]) for ln in spec["lines"]]
    return _key({"v": RENDER_VERSION, "kind": "audio", "ambient": ambient, "amb": amb, "lines": lines, "total": spec["total_frames"],
                 "lufs": LUFS_TARGET, "tp": TRUE_PEAK_TARGET, "sr": SAMPLE_RATE})


def _graph(spec: dict[str, Any], ambient: str, fps: int) -> tuple[list[str], str]:
    """返回 (输入文件参数, filter_complex 文本)；输出标签 [mix]。"""
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
            parts.append("[ambmix][dlg2]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=400[ambduck]")
            parts.append("[dlg1][ambduck]amix=inputs=2:normalize=0:duration=longest[premix]")
        else:
            parts.append("[dlgmix][ambmix]amix=inputs=2:normalize=0:duration=longest[premix]")
    else:
        parts.append("[dlgmix]anull[premix]")
    total_s = spec["total_frames"] / fps
    parts.append(f"[premix]asetpts=PTS-STARTPTS,apad=whole_dur={total_s:.4f}[padded]")
    return inputs, ";".join(parts)


def render_audio(spec: dict[str, Any], ambient: str, cache: Cache, fps: int) -> Node:
    key = audio_key(spec, ambient)
    dst = cache.path("audio", key, "wav")
    if dst.exists():
        return Node("audio_mix", ambient, key, True, str(dst))
    t0 = time.monotonic()
    inputs, graph = _graph(spec, ambient, fps)
    base = [media.FFMPEG, "-nostdin", "-v", "error", "-y", *inputs]
    # 第 1 遍：测量；第 2 遍：按测量值线性归一化
    measure = media._run([media.FFMPEG, "-nostdin", "-nostats", "-v", "info", "-y", *inputs, "-filter_complex", graph + f";[padded]loudnorm=I={LUFS_TARGET}:TP={TRUE_PEAK_TARGET}:LRA=11:print_format=json[mix]",
                                 "-map", "[mix]", "-f", "null", "-"], timeout=600)
    stats = json.loads(measure.stderr[measure.stderr.rindex("{"):])
    norm = (f"loudnorm=I={LUFS_TARGET}:TP={TRUE_PEAK_TARGET}:LRA=11:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:linear=true")
    tmp = dst.with_suffix(".part.wav")
    media._run(base + ["-filter_complex", graph + f";[padded]{norm},aresample={SAMPLE_RATE},asetpts=PTS-STARTPTS,apad=whole_dur={spec['total_frames'] / fps:.4f}[mix]", "-map", "[mix]", "-t", f"{spec['total_frames'] / fps:.4f}", "-ar", str(SAMPLE_RATE), "-ac", "2",
                            "-c:a", "pcm_s16le", str(tmp)], timeout=600)
    tmp.replace(dst)
    return Node("audio_mix", ambient, key, False, str(dst), round(time.monotonic() - t0, 2))


def premix_loudness(spec: dict[str, Any], ambient: str, cache: Cache, fps: int) -> float | None:
    """归一化之前的混音响度（LUFS）。off 档只有对白；其它档减去 off 档 = 混入原声后整体被抬高了多少 dB（越小说明原声相对对白越轻）。"""
    inputs, graph = _graph(spec, ambient, fps)
    tmp = cache.root / f"premix-{ambient}.wav"
    media._run([media.FFMPEG, "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[padded]", "-t", f"{spec['total_frames'] / fps:.4f}",
                "-c:a", "pcm_s16le", str(tmp)], timeout=600)
    try:
        return media.loudness(tmp).integrated_lufs
    finally:
        tmp.unlink(missing_ok=True)


# ---- 终混 ----


def aigc_metadata(produce_id: str) -> str:
    """隐式标识（容器元数据 AIGC）。字段取自《人工智能生成合成内容标识办法》配套标准的元数据格式，字段与取值待对照 GB 45438-2025 确认。"""
    return json.dumps({"Label": "1", "ContentProducer": "Dramio-P0-spike", "ProduceID": produce_id, "ReservedCode1": "", "ContentPropagator": "",
                       "PropagateID": "", "ReservedCode2": ""}, ensure_ascii=False, separators=(",", ":"))


def final_key(segment_keys: list[str], audio: str, ass_sha: str, badge_text: str, total_frames: int) -> str:
    return _key({"v": RENDER_VERSION, "kind": "final", "segments": segment_keys, "audio": audio, "ass": ass_sha, "badge": badge_text, "total": total_frames,
                 "crf": FINAL_CRF, "maxrate": FINAL_MAXRATE, "badge_font": BADGE_FONT_SIZE})


def render_final(spec: dict[str, Any], segments: list[Node], audio: Node, ass_path: Path, cache: Cache, out_path: Path, fps: int) -> Node:
    badge_text = spec["badge"]["text"]
    key = final_key([s.key for s in segments], audio.key, hashlib.sha256(ass_path.read_bytes()).hexdigest(), badge_text, spec["total_frames"])
    cached = cache.path("final", key, "mp4")
    if cached.exists():
        shutil.copyfile(cached, out_path)
        return Node("final", "final", key, True, str(cached))
    t0 = time.monotonic()
    listing = cache.root / f"concat-{key[:12]}.txt"
    listing.write_text("".join(f"file '{Path(s.path).resolve()}'\n" for s in segments), encoding="utf-8")
    vf = (f"subtitles={_esc(str(ass_path))}:fontsdir={_esc(str(Path(subtitles.FONT_FILE).parent))},"
          f"drawtext=fontfile={_esc(subtitles.FONT_FILE)}:text={_esc(badge_text)}:fontsize={BADGE_FONT_SIZE}:fontcolor=white:"
          f"box=1:boxcolor=black@0.45:boxborderw=14:x=w-tw-48:y=72")
    produce_id = _key([s.key for s in segments] + [audio.key])[:32]
    tmp = cache.path("final", key, "part.mp4")
    media._run([
        media.FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-i", audio.path,
        "-vf", vf, "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", str(FINAL_CRF), "-maxrate", FINAL_MAXRATE,
        "-bufsize", FINAL_BUFSIZE, "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", "160k", "-ar", str(SAMPLE_RATE),
        "-t", f"{spec['total_frames'] / fps:.4f}", "-metadata", f"AIGC={aigc_metadata(produce_id)}",
        "-metadata", "comment=AI generated content (Dramio P0 spike); visible label burned in",
        "-movflags", "+faststart+use_metadata_tags", str(tmp),
    ], timeout=1800)
    tmp.replace(cached)
    listing.unlink(missing_ok=True)
    shutil.copyfile(cached, out_path)
    return Node("final", "final", key, False, str(cached), round(time.monotonic() - t0, 2))


def render(spec: dict[str, Any], out_dir: Path, cache_dir: Path, ass_path: Path, ambient: str = "off", variants: tuple[str, ...] = ()) -> Result:
    """渲染成片到 out_dir/final.mp4；variants 里的原声档位只渲染音频（对比用），写 out_dir/audio/ambient-<档位>.wav 的缓存引用。"""
    if not media.available():
        raise media.MediaError("需要 ffmpeg / ffprobe")
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
