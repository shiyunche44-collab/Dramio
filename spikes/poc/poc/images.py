"""图像文件信息：格式与宽高（只用标准库，P0-06）。

JPEG 读 SOF 段（SOF0–SOF15，排除 DHT C4、JPG C8、DAC CC），PNG 读 IHDR。不解码像素。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

PNG_SIG = b"\x89PNG\r\n\x1a\n"
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_STANDALONE = {0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8}


class ImageFormatError(ValueError):
    pass


@dataclass(frozen=True)
class ImageInfo:
    fmt: str  # jpeg / png
    width: int
    height: int
    bytes: int
    sha256: str

    @property
    def pixels(self) -> int:
        return self.width * self.height


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            raise ImageFormatError(f"JPEG 段标记错误（偏移 {i}）")
        while i < n and data[i] == 0xFF:  # 填充字节
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1
        if marker in _STANDALONE:
            continue
        if marker == 0xD9:  # EOI
            break
        if i + 2 > n:
            break
        seg_len = struct.unpack(">H", data[i:i + 2])[0]
        if seg_len < 2:
            raise ImageFormatError("JPEG 段长度错误")
        if marker in _SOF:
            if i + 7 > n:
                break
            height, width = struct.unpack(">HH", data[i + 3:i + 7])
            if not width or not height:
                raise ImageFormatError("JPEG SOF 宽高为 0")
            return width, height
        i += seg_len
    raise ImageFormatError("JPEG 中没有 SOF 段")


def image_info(data: bytes) -> ImageInfo:
    if data[:2] == b"\xff\xd8":
        fmt = "jpeg"
        width, height = _jpeg_size(data)
    elif data[:8] == PNG_SIG:
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ImageFormatError("PNG 缺少 IHDR")
        fmt = "png"
        width, height = struct.unpack(">II", data[16:24])
    else:
        raise ImageFormatError("不是 JPEG 或 PNG")
    return ImageInfo(fmt, width, height, len(data), hashlib.sha256(data).hexdigest())


def parse_size(size: str) -> tuple[int, int] | None:
    """'1152x2048' → (1152, 2048)；档位（1K / 1.5K / 2K）返回 None。"""
    w, sep, h = size.lower().partition("x")
    if not sep or not w.isdigit() or not h.isdigit():
        return None
    return int(w), int(h)
