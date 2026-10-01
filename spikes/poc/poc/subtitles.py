"""P0-11 字幕：每句台词一条字幕（cue），按标点 / 字数换成至多两行；输出 ASS（烧入）与 SRT（给剪辑软件）。只用标准库。

字幕时间取该句 ASR 的语音起止（镜头内偏移 + 镜头起点），只有句级时间，没有字级对齐；长句内部不拆 cue。
几何：ASS 底部居中，边距由 STYLE 决定；`box()` 给出估算的字幕框（1080×1920 坐标），供与人脸框做相交检查。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from poc.compose import FPS, HEIGHT, WIDTH, Plan

FONT_NAME = "WenQuanYi Zen Hei"
FONT_FILE = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
FONT_SIZE = 60
MARGIN_V = 260  # 字幕底边到画面底边的距离（px）：避开竖屏平台底部的界面
LINE_SPACING = 1.25  # 行高 / 字号（估算框用）
MAX_CHARS = 14  # 单行最多字数；超过则换行
BREAK_AFTER = "，。！？、；：…"
_COLORS = {"dialogue": "&H00FFFFFF", "voiceover": "&H0099E6FF"}  # ASS 的 BGR：白色 / 淡黄（内心独白）


@dataclass(frozen=True)
class Cue:
    line_id: str
    shot_id: str
    kind: str
    text: str
    start_s: float  # 成片时间线上的绝对时间
    end_s: float

    @property
    def lines(self) -> list[str]:
        return wrap(self.text)


def wrap(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """至多两行：不超过 max_chars 不换行；否则在最接近中点的标点后换行（没有标点就从中点硬切）。超过两行容量（2 × max_chars）报错：ASS 不自动换行，会溢出画面。"""
    if len(text) > 2 * max_chars:
        raise ValueError(f"台词太长（{len(text)} 字，一条字幕最多 {2 * max_chars} 字）：{text[:20]}…")
    if len(text) <= max_chars:
        return [text]
    mid = len(text) / 2
    cuts = [i + 1 for i, ch in enumerate(text[:-1]) if ch in BREAK_AFTER]
    best = min(cuts, key=lambda c: abs(c - mid)) if cuts else int(mid)
    first, second = text[:best], text[best:]
    if len(first) > max_chars or len(second) > max_chars:  # 标点位置太偏时退回中点硬切
        first, second = text[: int(mid + 0.5)], text[int(mid + 0.5):]
    return [first, second]


def cues(plan: Plan) -> list[Cue]:
    out: list[Cue] = []
    for shot in plan.shots:
        base = shot.start_frames / FPS
        for ln in shot.lines:
            out.append(Cue(ln.line_id, shot.shot_id, ln.kind, ln.text, round(base + ln.speech_start_s, 3), round(base + ln.speech_end_s, 3)))
    return out


def ass_text(text: str) -> str:
    """ASS 文本转义：{ } 会开启覆盖标签、\\ 开头的是转义序列、换行会断开 Dialogue 行；统一换成全角 / 空格，行内换行由 wrap 用 \\N 控制。"""
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", " ").replace("\n", " ")


def _ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _srt_time(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def ass(items: list[Cue], font_name: str = FONT_NAME) -> str:
    styles = "\n".join(
        f"Style: {name},{font_name},{FONT_SIZE},{color},&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3.5,1,2,60,60,{MARGIN_V},1"
        for name, color in (("Dialogue", _COLORS["dialogue"]), ("Voiceover", _COLORS["voiceover"]))
    )
    head = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {WIDTH}\nPlayResY: {HEIGHT}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
        f"ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n{styles}\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    events = []
    for c in items:
        text = "\\N".join(ass_text(x) for x in c.lines)
        style = "Voiceover" if c.kind == "voiceover" else "Dialogue"
        events.append(f"Dialogue: 0,{_ass_time(c.start_s)},{_ass_time(c.end_s)},{style},{c.line_id},0,0,0,,{text}")
    return head + "\n".join(events) + "\n"


def srt(items: list[Cue]) -> str:
    return "\n".join(f"{i}\n{_srt_time(c.start_s)} --> {_srt_time(c.end_s)}\n" + "\n".join(c.lines) + "\n" for i, c in enumerate(items, 1))


def box(cue: Cue) -> dict[str, Any]:
    """估算字幕框（1080×1920 坐标）：全角字宽 ≈ 字号，水平居中，底边距画面底 MARGIN_V。"""
    lines = cue.lines
    w = max(len(line) for line in lines) * FONT_SIZE
    h = len(lines) * FONT_SIZE * LINE_SPACING
    x0 = (WIDTH - w) / 2
    y1 = HEIGHT - MARGIN_V
    return {"x0": round(x0, 1), "y0": round(y1 - h, 1), "x1": round(x0 + w, 1), "y1": float(y1), "lines": len(lines)}
