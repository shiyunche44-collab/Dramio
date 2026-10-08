"""P0-09 口型度量：嘴部开合序列、音频包络、起声偏差 / 相关 / 静音段张口（numpy + opencv + insightface 的 2d106det，只在分析真实视频时导入）。

判据（先于结果写进报告，见 docs/reports/p0/P0-09.md §3）：
- M2 起声偏差：嘴部开合起点与语音起点相差 ≤ 0.20 秒；
- M3 包络相关：语音窗口（前后各扩 0.25 秒）内音频包络与嘴部开合的皮尔逊相关 r（lag 0）高于该片段嘴部序列 200 次循环移位替身的 P95，
  并且比对照片段（P0-08 无台词片段 + P0-05 配音）至少高 0.15；同时报 ±0.25 秒内的最佳 lag；
- M4 静音段不张嘴：离语音窗口 ≥ 0.3 秒的帧上，平均开合 < 语音窗口内平均开合的 30%；
- 可测性：语音窗口内检出人脸的帧数 ≥ 80%，否则记“不可测”，不计入通过率。

嘴部开合 = 唇部关键点（106 点里的 52–71）的纵向范围 ÷ 脸宽（轮廓点 0–32 的横向范围），逐帧；未检出的帧线性插值（连续缺失 ≤ 3 帧），
再减去 10 分位数作基线，并做 3 帧滑动平均。度量只看开合的节奏，不判断口型（viseme）是否对得上字音。
"""

from __future__ import annotations

import contextlib
import io
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

FPS = 24
ONSET_TOL_S = 0.20
WINDOW_PAD_S = 0.25
SILENCE_MARGIN_S = 0.30
SILENCE_RATIO_MAX = 0.30
MEASURABLE_MIN = 0.80
SURROGATES = 200
LAG_FRAMES = 6  # ±0.25 秒
MAX_INTERP_GAP = 3
MODEL_ROOT = "~/.cache/poc-face"


def _np():
    import numpy as np

    return np


# ---- 音频包络 ----


def pcm(path: Path | str, rate: int = 16000):
    """ffmpeg 解出单声道 16 位 PCM → float 数组。"""
    np = _np()
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"], capture_output=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 解码失败：{proc.stderr.decode('utf-8', 'replace')[-200:]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(float)


def frame_envelope(x, rate: int = 16000, fps: int = FPS, n_frames: int | None = None):
    """每个视频帧对应的 RMS 包络（帧 k 覆盖 [k/fps, (k+1)/fps)）；n_frames 给定时补零 / 截断到该长度。"""
    np = _np()
    hop = rate / fps
    n = int(len(x) / hop) if n_frames is None else n_frames
    env = np.zeros(n)
    for k in range(n):
        seg = x[int(k * hop): int((k + 1) * hop)]
        if len(seg):
            env[k] = float(np.sqrt((seg ** 2).mean()))
    return env


# ---- 嘴部开合序列 ----


def smooth(x, width: int = 3):
    np = _np()
    if width <= 1 or len(x) == 0:
        return np.asarray(x, dtype=float)
    kernel = np.ones(width) / width
    pad = width // 2
    padded = np.pad(np.asarray(x, dtype=float), pad, mode="edge")
    return np.convolve(padded, kernel, mode="valid")[: len(x)]


def fill_gaps(values, max_gap: int = MAX_INTERP_GAP):
    """NaN 帧线性插值；连续缺失超过 max_gap 的段保持 NaN。返回 (序列, 检出掩码)。"""
    np = _np()
    v = np.asarray(values, dtype=float).copy()
    detected = ~np.isnan(v)
    if detected.sum() == 0:
        return v, detected
    idx = np.arange(len(v))
    nan = np.isnan(v)
    filled = np.interp(idx, idx[~nan], v[~nan])
    out = v.copy()
    i = 0
    while i < len(v):
        if nan[i]:
            j = i
            while j < len(v) and nan[j]:
                j += 1
            if j - i <= max_gap and i > 0 and j < len(v):
                out[i:j] = filled[i:j]
            i = j
        else:
            i += 1
    return out, detected


def normalize_opening(values):
    """开合序列：缺失帧保留 NaN，减去 10 分位数作基线（不小于 0），3 帧滑动平均（NaN 段不参与）。"""
    np = _np()
    v, _ = fill_gaps(values)
    ok = ~np.isnan(v)
    if ok.sum() == 0:
        return v
    base = float(np.percentile(v[ok], 10))
    out = np.full(len(v), np.nan)
    out[ok] = smooth(np.maximum(v[ok] - base, 0.0))
    return out


def lip_opening(landmarks) -> float:
    """106 点关键点 → 一帧的开合：唇部点（52–71）纵向范围 ÷ 脸宽（轮廓点 0–32 横向范围）。"""
    lips = landmarks[52:72]
    face_w = float(landmarks[0:33, 0].max() - landmarks[0:33, 0].min())
    if face_w <= 0:
        return float("nan")
    return float(lips[:, 1].max() - lips[:, 1].min()) / face_w


class MouthTracker:
    """insightface buffalo_l 的检测 + 2d106det（权重目录同 poc face：~/.cache/poc-face/models/buffalo_l）。"""

    def __init__(self, model_root: str = MODEL_ROOT, det_size: int = 640):
        import cv2
        import numpy  # noqa: F401
        from insightface.app import FaceAnalysis

        self._cv2 = cv2
        root = str(Path(model_root).expanduser())
        with contextlib.redirect_stdout(io.StringIO()):
            self._app = FaceAnalysis(name="buffalo_l", root=root, allowed_modules=["detection", "landmark_2d_106"], providers=["CPUExecutionProvider"])
            self._app.prepare(ctx_id=-1, det_size=(det_size, det_size))

    def frames(self, video: Path | str):
        cap = self._cv2.VideoCapture(str(video))
        try:
            while True:
                ok, img = cap.read()
                if not ok:
                    return
                yield img
        finally:
            cap.release()

    def opening_series(self, video: Path | str):
        """逐帧开合（未检出为 NaN）与每帧嘴部包围盒（给缩略条用）。取检测分最高的人脸。"""
        np = _np()
        values, boxes = [], []
        for img in self.frames(video):
            faces = self._app.get(img)
            if not faces:
                values.append(float("nan"))
                boxes.append(None)
                continue
            f = max(faces, key=lambda x: x.det_score)
            lm = f.landmark_2d_106
            values.append(lip_opening(lm))
            lips = lm[52:72]
            boxes.append((float(lips[:, 0].min()), float(lips[:, 1].min()), float(lips[:, 0].max()), float(lips[:, 1].max())))
        return np.asarray(values), boxes


# ---- 判据 ----


def pearson(a, b) -> float:
    np = _np()
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 4:
        return float("nan")
    a, b = a[ok], b[ok]
    if a.std() < 1e-12 or b.std() < 1e-12:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def window_frames(speech: list[tuple[float, float]], n: int, fps: int = FPS, pad_s: float = WINDOW_PAD_S) -> tuple[int, int]:
    """语音窗口（所有句子的外包络，前后各扩 pad_s）的帧区间 [k0, k1)。"""
    t0 = min(s for s, _ in speech)
    t1 = max(e for _, e in speech)
    k0 = max(0, int((t0 - pad_s) * fps))
    k1 = min(n, int((t1 + pad_s) * fps) + 1)
    return k0, max(k0 + 1, k1)


def speech_mask(speech: list[tuple[float, float]], n: int, fps: int = FPS, margin_s: float = 0.0):
    np = _np()
    mask = np.zeros(n, dtype=bool)
    for s, e in speech:
        mask[max(0, int((s - margin_s) * fps)): min(n, int((e + margin_s) * fps) + 1)] = True
    return mask


def mouth_onset(opening, speech: list[tuple[float, float]], fps: int = FPS) -> float | None:
    """嘴部开合起点：从语音起点前 1 秒起，第一次连续 2 帧超过（语音窗口内 P95 的 35%）。"""
    np = _np()
    n = len(opening)
    k0, k1 = window_frames(speech, n, fps)
    inside = opening[k0:k1]
    inside = inside[~np.isnan(inside)]
    if len(inside) == 0:
        return None
    thr = 0.35 * float(np.percentile(inside, 95))
    if thr <= 0:
        return None
    start = max(0, int((min(s for s, _ in speech) - 1.0) * fps))
    for k in range(start, n - 1):
        if opening[k] > thr and opening[k + 1] > thr:
            return round(k / fps, 4)
    return None


@dataclass
class ClipMetrics:
    measurable: bool
    detected_frac: float  # 语音窗口内检出人脸的帧占比
    speech_onset_s: float | None = None
    mouth_onset_s: float | None = None
    onset_delta_s: float | None = None  # M2：嘴部起点 − 语音起点
    r0: float | None = None  # M3：lag 0 的相关
    surrogate_p95: float | None = None
    best_lag_s: float | None = None
    best_r: float | None = None
    silence_ratio: float | None = None  # M4
    m2_pass: bool | None = None
    m3_pass: bool | None = None  # 只含“高于替身 P95”；“比对照高 0.15”在汇总时判
    m4_pass: bool | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate(opening_raw, audio_env, speech: list[tuple[float, float]], fps: int = FPS, seed: int = 0) -> ClipMetrics:
    """一个片段的 M2 / M3 / M4。opening_raw：逐帧开合（NaN 为未检出）；audio_env：逐帧音频包络；speech：语音起止（秒）。"""
    np = _np()
    n = min(len(opening_raw), len(audio_env))
    raw = np.asarray(opening_raw[:n], dtype=float)
    env = np.asarray(audio_env[:n], dtype=float)
    k0, k1 = window_frames(speech, n, fps)
    in_win = raw[k0:k1]
    frac = float((~np.isnan(in_win)).mean()) if len(in_win) else 0.0
    m = ClipMetrics(measurable=frac >= MEASURABLE_MIN, detected_frac=round(frac, 4))
    if not m.measurable:
        m.notes.append(f"语音窗口内只有 {frac:.0%} 的帧检出人脸，不可测")
        return m
    op = normalize_opening(raw)
    m.speech_onset_s = round(min(s for s, _ in speech), 4)
    m.mouth_onset_s = mouth_onset(op, speech, fps)
    if m.mouth_onset_s is not None:
        m.onset_delta_s = round(m.mouth_onset_s - m.speech_onset_s, 4)
        m.m2_pass = abs(m.onset_delta_s) <= ONSET_TOL_S + 1e-9
    else:
        m.m2_pass = False
        m.notes.append("没有检出嘴部开合起点")
    env_s = smooth(env, 3)
    m.r0 = _round(pearson(env_s[k0:k1], op[k0:k1]))
    best = (None, None)
    for lag in range(-LAG_FRAMES, LAG_FRAMES + 1):
        shifted = np.roll(op, lag)
        r = pearson(env_s[k0:k1], shifted[k0:k1])
        if not np.isnan(r) and (best[1] is None or r > best[1]):
            best = (lag, r)
    if best[0] is not None:
        m.best_lag_s, m.best_r = round(best[0] / fps, 4), _round(best[1])
    rng = np.random.default_rng(seed)
    shifts = rng.integers(max(1, int(0.5 * fps)), max(2, n - int(0.5 * fps)), size=SURROGATES)
    sur = [pearson(env_s[k0:k1], np.roll(op, int(s))[k0:k1]) for s in shifts]
    sur = [x for x in sur if not np.isnan(x)]
    if sur:
        m.surrogate_p95 = _round(float(np.percentile(sur, 95)))
    if m.r0 is not None and m.surrogate_p95 is not None:
        m.m3_pass = m.r0 > m.surrogate_p95
    quiet = ~speech_mask(speech, n, fps, margin_s=SILENCE_MARGIN_S) & ~np.isnan(op)
    talk = speech_mask(speech, n, fps) & ~np.isnan(op)
    if quiet.sum() >= 3 and talk.sum() >= 3 and op[talk].mean() > 0:
        m.silence_ratio = _round(float(op[quiet].mean() / op[talk].mean()))
        m.m4_pass = m.silence_ratio < SILENCE_RATIO_MAX
    else:
        m.notes.append("静音帧不足 3 帧，M4 不判")
    return m


def _round(x: float | None, digits: int = 4) -> float | None:
    return None if x is None or x != x else round(float(x), digits)


# ---- 绘图 / 缩略条（只用 opencv） ----


def curve_image(opening, env, speech: list[tuple[float, float]], path: Path, fps: int = FPS, size=(960, 240)) -> None:
    """嘴部开合（红）与音频包络（蓝）叠在一张图上，灰色带为语音窗口。"""
    import cv2

    np = _np()
    w, h = size
    img = np.full((h, w, 3), 255, dtype=np.uint8)
    n = min(len(opening), len(env))
    if n < 2:
        return
    for s, e in speech:
        cv2.rectangle(img, (int(s * fps / n * w), 0), (int(e * fps / n * w), h), (225, 225, 225), -1)

    def poly(series, color):
        arr = np.asarray(series[:n], dtype=float)
        top = np.nanmax(arr) if np.isfinite(arr).any() else 1.0
        top = top if top > 0 else 1.0
        pts = [(int(i / (n - 1) * (w - 1)), int(h - 8 - (v / top) * (h - 16))) for i, v in enumerate(arr) if not np.isnan(v)]
        if len(pts) > 1:
            cv2.polylines(img, [np.array(pts, dtype=np.int32)], False, color, 2)

    poly(env, (200, 90, 20))  # 蓝（BGR）
    poly(opening, (40, 40, 220))  # 红
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)


def mouth_strip(tracker: MouthTracker, video: Path | str, boxes, speech: list[tuple[float, float]], path: Path, fps: int = FPS, count: int = 12, tile: int = 112) -> None:
    """语音窗口内均匀取 count 帧，裁嘴部（脸下半部）横向拼成缩略条。"""
    import cv2

    np = _np()
    frames = list(tracker.frames(video))
    k0, k1 = window_frames(speech, len(frames), fps, pad_s=0.0)
    picks = np.linspace(k0, max(k0, k1 - 1), count).round().astype(int)
    tiles = []
    for k in picks:
        img, box = frames[min(k, len(frames) - 1)], boxes[min(k, len(boxes) - 1)]
        if box is None:
            tiles.append(np.zeros((tile, tile, 3), dtype=np.uint8))
            continue
        x0, y0, x1, y1 = box
        cx, cy, half = (x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0) * 0.9
        crop = img[max(0, int(cy - half)): int(cy + half), max(0, int(cx - half)): int(cx + half)]
        tiles.append(cv2.resize(crop, (tile, tile)) if crop.size else np.zeros((tile, tile, 3), dtype=np.uint8))
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 80])
