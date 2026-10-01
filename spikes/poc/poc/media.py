"""media：ffprobe / ffmpeg 封装，度量生成视频（P0-08，只用标准库 + 系统 ffmpeg，不依赖 numpy）。

- probe      实际分辨率、时长、帧率、码率、视频 / 音频编码、是否有音频流（ffprobe -show_format -show_streams）；
- extract_frames  抽帧：按比例 0 / 25% / 50% / 75% / 末帧（帧序号 = 四舍五入（0.5 进位）(比例 × (总帧数 − 1))），输出 jpg；
- ssim       两张图的 SSIM（ffmpeg 的 ssim 滤镜，取 All 分量）；比较前把参考图缩放到对比图的尺寸，
             首帧保真度 = SSIM(P0-07 选定的首帧, 视频第 0 帧)；
- motion     运动量代理：相邻帧的平均亮度差（signalstats 的 YDIF，0–255 刻度，先缩到 160 宽的灰度图），
             返回均值 / P95 / 最大值；静止画面接近 0，数值只用于同一批视频之间的相对比较，没有绝对意义；
- trim       裁剪到指定时长：重编码（libx264 crf 18 + aac），保证帧数精确；源视频不长于目标时直接复制，
             两种情况都在 TrimResult.reencoded 里记录；
- contact_sheet  把若干帧横向拼成一张缩略条（证据入库用，体积很小）。

没有 ffmpeg / ffprobe 时 available() 为 False，调用各函数会抛 MediaError（单元测试据此 skip）。
所有函数只读本地文件、只调用本机 ffmpeg，不联网。
"""

from __future__ import annotations

import json
import re
import shutil
import statistics
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)
FRACTION_LABELS = {0.0: "f000", 0.25: "f025", 0.5: "f050", 0.75: "f075", 1.0: "f100"}
MOTION_WIDTH = 160
DEFAULT_TIMEOUT = 300.0
_SSIM_RE = re.compile(r"All:([0-9.]+|nan|inf)")
_YDIF_RE = re.compile(r"lavfi\.signalstats\.YDIF=([0-9.eE+-]+)")


class MediaError(Exception):
    """ffmpeg / ffprobe 不可用、命令失败、输出无法解析，或文件不是可解析的媒体。"""


def available() -> bool:
    return shutil.which(FFMPEG) is not None and shutil.which(FFPROBE) is not None


def _run(cmd: list[str], timeout: float = DEFAULT_TIMEOUT, cwd: Path | str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace", cwd=cwd)
    except FileNotFoundError:
        raise MediaError(f"找不到 {cmd[0]}（需要安装 ffmpeg）") from None
    except subprocess.TimeoutExpired:
        raise MediaError(f"{cmd[0]} 超时（{timeout:g}s）") from None
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise MediaError(f"{cmd[0]} 失败（退出码 {proc.returncode}）：{' | '.join(tail)[:300]}")
    return proc


# ---- ffprobe ----


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    duration_s: float  # 视频流时长；没有则取容器时长
    format_duration_s: float | None
    fps: float | None
    bit_rate: int | None  # 容器总码率（bit/s）
    size_bytes: int
    video_codec: str
    audio_codec: str | None
    has_audio: bool
    nb_frames: int | None
    pix_fmt: str | None
    container: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _float(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _int(value: Any) -> int | None:
    x = _float(value)
    return int(x) if x is not None else None


def _rate(text: Any) -> float | None:
    """'24/1' / '30000/1001' / '24' → 帧率；'0/0' 视为没有。"""
    if not isinstance(text, str):
        return _float(text)
    num, sep, den = text.partition("/")
    n, d = _float(num), _float(den) if sep else 1.0
    if n is None or not d:
        return None
    return round(n / d, 3)


def probe(path: Path | str) -> MediaInfo:
    """用 ffprobe 读取实际参数；文件不是可解析的视频（没有视频流）时抛 MediaError。"""
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"文件不存在：{path}")
    proc = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=60)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise MediaError("ffprobe 输出不是 JSON") from None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise MediaError("文件中没有视频流")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    width, height = _int(video.get("width")), _int(video.get("height"))
    if not width or not height:
        raise MediaError("视频流缺少宽高")
    fmt_dur = _float(fmt.get("duration"))
    dur = _float(video.get("duration")) or fmt_dur
    if dur is None:
        raise MediaError("无法确定视频时长")
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    nb = _int(video.get("nb_frames"))
    if nb is None:  # 少数容器没有帧数：数一遍（短视频，很快）
        counted = _run(
            [FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)], timeout=120,
        )
        nb = _int(counted.stdout.strip())
    return MediaInfo(
        width=width, height=height, duration_s=round(dur, 4), format_duration_s=round(fmt_dur, 4) if fmt_dur is not None else None,
        fps=fps, bit_rate=_int(fmt.get("bit_rate")), size_bytes=_int(fmt.get("size")) or path.stat().st_size,
        video_codec=str(video.get("codec_name") or ""), audio_codec=str(audio.get("codec_name")) if audio else None,
        has_audio=audio is not None, nb_frames=nb, pix_fmt=video.get("pix_fmt"), container=fmt.get("format_name"),
    )


def image_size(path: Path | str) -> tuple[int, int]:
    """图片（jpg / png）的宽高，ffprobe 读取。"""
    info = probe(path)
    return info.width, info.height


# ---- 抽帧 ----


@dataclass(frozen=True)
class Frame:
    label: str  # f000 / f025 / f050 / f075 / f100
    fraction: float
    index: int  # 帧序号（从 0 起）
    time_s: float | None
    path: Path


def frame_indices(nb_frames: int, fractions: tuple[float, ...] = FRACTIONS) -> list[int]:
    """按比例取帧序号：四舍五入（0.5 进位）(比例 × (总帧数 − 1))；总帧数不足时相邻比例会落在同一帧，保留重复以便逐比例对应。"""
    if nb_frames < 1:
        raise MediaError("视频没有帧")
    return [min(nb_frames - 1, max(0, int(f * (nb_frames - 1) + 0.5))) for f in fractions]


def extract_frames(
    path: Path | str, out_dir: Path | str, info: MediaInfo | None = None, fractions: tuple[float, ...] = FRACTIONS
) -> list[Frame]:
    """抽帧为 jpg（文件名 f000.jpg …），返回按比例排序的列表；抽不出来的帧抛 MediaError。"""
    path, out_dir = Path(path), Path(out_dir)
    info = info or probe(path)
    if not info.nb_frames:
        raise MediaError("无法确定总帧数")
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for fraction, idx in zip(fractions, frame_indices(info.nb_frames, fractions)):
        label = FRACTION_LABELS.get(fraction) or f"f{round(fraction * 100):03d}"
        dst = out_dir / f"{label}.jpg"
        if dst.exists():
            dst.unlink()
        _run(
            [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(path), "-vf", f"select=eq(n\\,{idx})", "-fps_mode", "passthrough",
             "-frames:v", "1", "-q:v", "2", str(dst)], timeout=120,
        )
        if not dst.exists() or dst.stat().st_size == 0:
            raise MediaError(f"第 {idx} 帧抽取失败")
        frames.append(Frame(label, fraction, idx, round(idx / info.fps, 4) if info.fps else None, dst))
    return frames


# ---- SSIM 与运动量 ----


def ssim(reference: Path | str, other: Path | str, size: tuple[int, int] | None = None) -> float:
    """SSIM（All 分量，0–1）。两边先缩放到 size（默认取 other 的尺寸）并转 yuv420p，再用 ffmpeg 的 ssim 滤镜比较。

    other 可以是图片或视频（视频取第一帧序列逐帧比较时结果是各帧平均，首帧保真请传抽出的第 0 帧图片）。
    """
    width, height = size or image_size(other)
    chain = f"scale={width}:{height}:flags=bicubic,format=yuv420p"
    proc = _run(
        [FFMPEG, "-nostdin", "-hide_banner", "-i", str(reference), "-i", str(other), "-lavfi",
         f"[0:v]{chain}[a];[1:v]{chain}[b];[a][b]ssim", "-f", "null", "-"], timeout=120,
    )
    matches = _SSIM_RE.findall(proc.stderr)
    value = _float(matches[-1]) if matches else None
    if value is None:
        raise MediaError("无法解析 SSIM 输出")
    return round(value, 6)


@dataclass(frozen=True)
class Motion:
    mean: float  # 相邻帧平均亮度差（0–255）的均值
    p95: float
    max: float
    pairs: int  # 相邻帧对数

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def motion(path: Path | str) -> Motion:
    """运动量代理：相邻帧 Y 通道平均绝对差（signalstats 的 YDIF）。只有一帧时抛 MediaError。"""
    proc = _run(
        [FFMPEG, "-nostdin", "-nostats", "-v", "error", "-i", str(path), "-vf",
         f"scale={MOTION_WIDTH}:-2,format=gray,signalstats,metadata=print:key=lavfi.signalstats.YDIF:file=-", "-f", "null", "-"],
        timeout=180,
    )
    values = [float(v) for v in _YDIF_RE.findall(proc.stdout)][1:]  # 第 0 帧没有前一帧，YDIF 恒为 0
    if not values:
        raise MediaError("视频不足两帧，无法计算运动量")
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return Motion(round(statistics.fmean(values), 4), round(p95, 4), round(ordered[-1], 4), len(values))


# ---- 裁剪与缩略条 ----


@dataclass(frozen=True)
class TrimResult:
    path: Path
    reencoded: bool
    source_duration_s: float
    duration_s: float
    target_s: float

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "path": str(self.path)}


def trim(src: Path | str, dst: Path | str, seconds: float, info: MediaInfo | None = None) -> TrimResult:
    """裁剪到 seconds 秒。源视频不长于目标（容差半帧）时原样复制（reencoded=False）；否则重编码（reencoded=True）。"""
    src, dst = Path(src), Path(dst)
    if seconds <= 0:
        raise MediaError("目标时长必须大于 0")
    info = info or probe(src)
    tolerance = 0.5 / info.fps if info.fps else 0.05
    dst.parent.mkdir(parents=True, exist_ok=True)
    if info.duration_s <= seconds + tolerance:
        shutil.copyfile(src, dst)
        return TrimResult(dst, False, info.duration_s, info.duration_s, seconds)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(src), "-t", f"{seconds:g}", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "18", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k"] if info.has_audio else ["-an"]
    _run(cmd + ["-movflags", "+faststart", str(dst)], timeout=300)
    out = probe(dst)
    return TrimResult(dst, True, info.duration_s, out.duration_s, seconds)


def contact_sheet(frames: list[Path | str], out: Path | str, tile_width: int = 160, tile_height: int | None = None) -> Path:
    """把若干帧缩到同一宽度后横向拼成一张 jpg（帧数 ≥ 1）。比例略有差异的帧（如 768×1344 与 1152×2048）要传 tile_height 才能 hstack。"""
    if not frames:
        raise MediaError("没有帧可拼")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y"]
    for f in frames:
        cmd += ["-i", str(f)]
    height = tile_height if tile_height else -2
    scaled = ";".join(f"[{i}:v]scale={tile_width}:{height}[s{i}]" for i in range(len(frames)))
    if len(frames) == 1:
        graph, label = scaled, "[s0]"
    else:
        graph = scaled + ";" + "".join(f"[s{i}]" for i in range(len(frames))) + f"hstack=inputs={len(frames)}[o]"
        label = "[o]"
    _run(cmd + ["-filter_complex", graph, "-map", label, "-frames:v", "1", "-q:v", "4", str(out)], timeout=120)
    if not out.exists() or out.stat().st_size == 0:
        raise MediaError("缩略条生成失败")
    return out


# ---- 音频与响度（P0-11） ----

_LUFS_RE = re.compile(r"^\s+I:\s+(-?[0-9.]+|-inf)\s+LUFS", re.MULTILINE)
_TPEAK_RE = re.compile(r"^\s+Peak:\s+(-?[0-9.]+|-inf)\s+dBFS", re.MULTILINE)
_LRA_RE = re.compile(r"^\s+LRA:\s+(-?[0-9.]+)\s+LU\s*$", re.MULTILINE)
_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?[0-9.]+|-inf)\s+dB")
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")


@dataclass(frozen=True)
class AudioInfo:
    codec: str
    sample_rate: int
    channels: int
    duration_s: float | None


def probe_audio(path: Path | str) -> AudioInfo | None:
    """第一条音频流的编码、采样率、声道数、时长；没有音频流返回 None（视频或纯音频文件都行）。"""
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"文件不存在：{path}")
    proc = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-select_streams", "a:0", str(path)], timeout=60)
    try:
        streams = json.loads(proc.stdout).get("streams") or []
    except ValueError:
        raise MediaError("ffprobe 输出不是 JSON") from None
    if not streams:
        return None
    s = streams[0]
    return AudioInfo(str(s.get("codec_name") or ""), _int(s.get("sample_rate")) or 0, _int(s.get("channels")) or 0, _float(s.get("duration")))


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float | None  # 整体响度（EBU R128）；静音或太短为 None
    true_peak_dbtp: float | None  # 真峰值（ebur128 peak=true）
    loudness_range_lu: float | None = None  # 响度范围 LRA（P0-10 加，默认 None 以兼容旧调用）


def loudness(path: Path | str, stream: str = "a:0", start_s: float | None = None, dur_s: float | None = None) -> Loudness:
    """整体响度、真峰值与 LRA（ffmpeg ebur128，peak=true）。start_s / dur_s 只量一个时间窗（输入侧 -ss / -t）。"""
    window: list[str] = []
    if start_s is not None:
        window += ["-ss", f"{start_s:.3f}"]
    if dur_s is not None:
        window += ["-t", f"{dur_s:.3f}"]
    proc = _run([FFMPEG, "-nostdin", "-nostats", "-hide_banner", *window, "-i", str(path), "-map", stream, "-af", "ebur128=peak=true", "-f", "null", "-"], timeout=300)
    summary = proc.stderr.rsplit("Summary:", 1)[-1]  # 只取末尾汇总，不取逐帧输出
    lufs, peak, lra = _LUFS_RE.search(summary), _TPEAK_RE.search(summary), _LRA_RE.search(summary)
    return Loudness(_float(lufs.group(1)) if lufs else None, _float(peak.group(1)) if peak else None, _float(lra.group(1)) if lra else None)


def mean_volume_db(path: Path | str, start_s: float | None = None, dur_s: float | None = None) -> float | None:
    """平均音量（dB，ffmpeg volumedetect，即全段 RMS 的 dB 值，没有响度门限）。start_s / dur_s 只量一个时间窗；全静音为 None。"""
    window: list[str] = []
    if start_s is not None:
        window += ["-ss", f"{start_s:.3f}"]
    if dur_s is not None:
        window += ["-t", f"{dur_s:.3f}"]
    proc = _run([FFMPEG, "-nostdin", "-nostats", "-hide_banner", *window, "-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"], timeout=300)
    m = _MEAN_VOLUME_RE.search(proc.stderr)
    return _float(m.group(1)) if m else None


def format_duration(path: Path | str) -> float | None:
    """容器级时长（秒）；部分 mp3 / wav 的音频流没有 duration，用它兜底。"""
    proc = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", str(path)], timeout=60)
    try:
        return _float((json.loads(proc.stdout).get("format") or {}).get("duration"))
    except ValueError:
        raise MediaError("ffprobe 输出不是 JSON") from None


def silences(path: Path | str, noise_db: float = -50.0, min_s: float = 0.1) -> list[tuple[float, float | None]]:
    """静音段 (起, 止)（ffmpeg silencedetect）；文件末尾仍在静音中时止为 None。"""
    proc = _run([FFMPEG, "-nostdin", "-nostats", "-hide_banner", "-i", str(path), "-map", "0:a:0", "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"], timeout=300)
    out: list[tuple[float, float | None]] = []
    for line in proc.stderr.splitlines():
        m_start, m_end = _SILENCE_START_RE.search(line), _SILENCE_END_RE.search(line)
        if m_start:
            out.append((max(0.0, float(m_start.group(1))), None))
        elif m_end and out and out[-1][1] is None:
            out[-1] = (out[-1][0], float(m_end.group(1)))
    return out


def format_tags(path: Path | str) -> dict[str, str]:
    """容器级元数据标签（键统一小写）。"""
    proc = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", str(path)], timeout=60)
    try:
        tags = (json.loads(proc.stdout).get("format") or {}).get("tags") or {}
    except ValueError:
        raise MediaError("ffprobe 输出不是 JSON") from None
    return {str(k).lower(): str(v) for k, v in tags.items()}


_VERSION: list[str] = []


def version() -> str:
    """`ffmpeg -version` 的第一行（进缓存 key：版本变了，输出字节可能变）；没有 ffmpeg 返回空串。"""
    if not _VERSION:
        try:
            first = _run([FFMPEG, "-version"], timeout=30).stdout.splitlines()
        except MediaError:
            first = []
        _VERSION.append(first[0] if first else "")
    return _VERSION[0]
