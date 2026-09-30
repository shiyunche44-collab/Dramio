"""离线音频工具（只用标准库，P0-05）：MP3 帧头时长、逐句拼接、字错率（CER）。

MP3 时长按帧计数：每帧采样数 MPEG-1 Layer III 为 1152，MPEG-2 / 2.5 Layer III 为 576
（豆包语音 24 kHz 输出属于 MPEG-2），时长 = 帧数 × 每帧采样数 / 采样率。
开头的 ID3v2 标签和 Xing / Info 头帧（不含音频）不计入时长。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# 比特率（kbps），下标为帧头中的 4 位比特率索引；0 与 15 不合法
_BITRATES_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_BITRATES_V2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}  # 版本位 → 采样率


class MP3Error(ValueError):
    pass


@dataclass(frozen=True)
class MP3Info:
    frames: int
    sample_rate: int
    samples_per_frame: int
    version: str  # "1" / "2" / "2.5"
    bitrate_kbps_avg: float
    duration_s: float
    id3_bytes: int
    xing: bool


@dataclass(frozen=True)
class _Frame:
    offset: int
    length: int
    version_bits: int
    sample_rate: int
    samples: int
    bitrate: int


def id3v2_size(data: bytes) -> int:
    """开头 ID3v2 标签的总字节数（含 10 字节头、可选的 10 字节尾）；没有标签时为 0。"""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = 0
    for b in data[6:10]:
        if b & 0x80:
            raise MP3Error("ID3v2 长度字段不是 syncsafe 整数")
        size = (size << 7) | b
    footer = 10 if data[5] & 0x10 else 0
    return 10 + size + footer


def _parse_header(data: bytes, pos: int) -> _Frame | None:
    if pos + 4 > len(data):
        return None
    b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
    if data[pos] != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version_bits = (b1 >> 3) & 0x3
    layer_bits = (b1 >> 1) & 0x3
    if version_bits == 1 or layer_bits != 1:  # 保留版本，或不是 Layer III
        return None
    bitrate_idx = (b2 >> 4) & 0xF
    sr_idx = (b2 >> 2) & 0x3
    if bitrate_idx in (0, 15) or sr_idx == 3:
        return None
    padding = (b2 >> 1) & 0x1
    sample_rate = _SAMPLE_RATES[version_bits][sr_idx]
    if version_bits == 3:
        bitrate = _BITRATES_V1_L3[bitrate_idx] * 1000
        samples = 1152
        length = 144 * bitrate // sample_rate + padding
    else:
        bitrate = _BITRATES_V2_L3[bitrate_idx] * 1000
        samples = 576
        length = 72 * bitrate // sample_rate + padding
    del b3
    return _Frame(pos, length, version_bits, sample_rate, samples, bitrate)


def _side_info_len(frame: _Frame, data: bytes) -> int:
    mono = (data[frame.offset + 3] >> 6) == 3
    if frame.version_bits == 3:
        return 17 if mono else 32
    return 9 if mono else 17


def _is_xing(frame: _Frame, data: bytes) -> bool:
    start = frame.offset + 4 + _side_info_len(frame, data)
    return data[start : start + 4] in (b"Xing", b"Info")


def frames(data: bytes) -> tuple[int, list[_Frame]]:
    """(ID3v2 字节数, 连续的帧列表)。帧之间不允许有垃圾字节；末尾不完整的帧视为错误。"""
    pos = id3v2_size(data)
    start = pos
    out: list[_Frame] = []
    while pos < len(data):
        if data[pos : pos + 3] == b"TAG" and len(data) - pos == 128:  # ID3v1 尾标签
            break
        frame = _parse_header(data, pos)
        if frame is None:
            raise MP3Error(f"偏移 {pos} 处不是合法的 MPEG Layer III 帧头")
        if pos + frame.length > len(data):
            raise MP3Error(f"偏移 {pos} 处的帧不完整（需要 {frame.length} 字节，剩余 {len(data) - pos}）")
        if out and (frame.sample_rate != out[0].sample_rate or frame.version_bits != out[0].version_bits):
            raise MP3Error(f"偏移 {pos} 处的帧采样率或版本与首帧不一致")
        out.append(frame)
        pos += frame.length
    return start, out


def mp3_info(data: bytes) -> MP3Info:
    id3, fs = frames(data)
    if not fs:
        raise MP3Error("没有音频帧")
    xing = _is_xing(fs[0], data)
    audio = fs[1:] if xing else fs
    if not audio:
        raise MP3Error("只有 Xing / Info 头帧，没有音频帧")
    first = audio[0]
    samples = sum(f.samples for f in audio)
    duration = samples / first.sample_rate
    version = {3: "1", 2: "2", 0: "2.5"}[first.version_bits]
    avg_kbps = sum(f.length for f in audio) * 8 / duration / 1000
    return MP3Info(len(audio), first.sample_rate, first.samples, version, round(avg_kbps, 1), duration, id3, xing)


def audio_frames(data: bytes) -> bytes:
    """去掉 ID3 标签和 Xing / Info 头帧后的纯音频帧字节，用于拼接。"""
    _, fs = frames(data)
    if fs and _is_xing(fs[0], data):
        fs = fs[1:]
    if not fs:
        raise MP3Error("没有音频帧")
    return data[fs[0].offset : fs[-1].offset + fs[-1].length]


def concat(parts: list[bytes]) -> bytes:
    """按顺序拼接多段 MP3（要求采样率与版本一致），结果不含 ID3 标签。"""
    infos = [mp3_info(p) for p in parts]
    if len({(i.sample_rate, i.version) for i in infos}) > 1:
        raise MP3Error("各段的采样率或 MPEG 版本不一致，不能直接拼接")
    return b"".join(audio_frames(p) for p in parts)


# ---- 字错率 ----


def normalize(text: str) -> str:
    """CER 归一化：NFKC（全角转半角）、小写，去掉标点、符号和空白。"""
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if not ch.isspace() and unicodedata.category(ch)[0] not in "PSZ")


@dataclass(frozen=True)
class CER:
    ref_len: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def rate(self) -> float:
        if self.ref_len == 0:
            return 0.0 if self.errors == 0 else 1.0
        return self.errors / self.ref_len


def cer(ref: str, hyp: str) -> CER:
    """字级编辑距离，返回替换 / 删除 / 插入数（同等代价时优先替换，再删除）。"""
    n, m = len(ref), len(hyp)
    # dp[i][j] = (距离, 替换, 删除, 插入)
    prev = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [(i, 0, i, 0)]
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                cur.append(prev[j - 1])
                continue
            s, d, ins = prev[j - 1], prev[j], cur[j - 1]
            cur.append(
                min(
                    (s[0] + 1, s[1] + 1, s[2], s[3]),
                    (d[0] + 1, d[1], d[2] + 1, d[3]),
                    (ins[0] + 1, ins[1], ins[2], ins[3] + 1),
                    key=lambda t: t[0],
                )
            )
        prev = cur
    _, sub, dele, ins = prev[m]
    return CER(n, sub, dele, ins)


# ASR 无法从读音区分的同音字：按一个字比较（只用于“等价折叠 CER”，原 CER 不受影响）
# 第三人称代词（tā）与结构助词（de）
_HOMOPHONE_FOLD = str.maketrans({"她": "他", "它": "他", "祂": "他", "得": "的", "地": "的"})


def fold_equivalents(text: str) -> str:
    """在 normalize 之后使用：把 ASR 听不出区别的同音代词、结构助词折叠成同一个字。"""
    return text.translate(_HOMOPHONE_FOLD)


def has_digit_mismatch(ref: str, hyp: str) -> bool:
    """转写里出现了原文没有的阿拉伯数字（例如“三”被写成“3”），这类差异在明细中标为等价差异。"""
    return any(ch.isdigit() for ch in hyp) and not any(ch.isdigit() for ch in ref)
