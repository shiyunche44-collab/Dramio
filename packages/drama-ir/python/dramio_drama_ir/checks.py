"""DramaIR 校验：先做 Schema 结构校验，结构无误后再做语义校验。

语义规则分两级：
- 错误：文档不可用（引用不存在、id 重复、数值越界……）
- 警告：文档可用但可能有问题（时长偏差、台词念不完……）；--strict 时也算失败

阈值是 P0 的经验值，集中定义在下方，P0-14 复盘时修订。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from dramio_drama_ir.report import Issue, Report
from dramio_drama_ir.schema import load_schema, structural_issues

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
IR_VERSION_PATTERN = re.compile(r"^0\.\d+\.\d+$")  # v0 草案
MAX_SHOT_S = 10.0  # 单个镜头上限，与单次视频生成上限一致（architecture §5.3）
SPEED_RANGE = (0.5, 2.0)
DURATION_TOLERANCE = 0.20  # 镜头时长合计与目标时长的允许偏差
CHARS_PER_SECOND = 4.5  # 中文朗读速度估算（每秒字数，不含标点和空白）


def validate(doc: Any, schema: dict[str, Any] | None = None) -> Report:
    report = Report()
    report.errors.extend(structural_issues(doc, load_schema("v0") if schema is None else schema))
    if not report.errors:  # 语义校验依赖正确的结构
        _Semantic(doc, report).run()
    return report


def speech_chars(text: str) -> int:
    """估算朗读字数：不计标点、符号和空白。"""
    return sum(1 for ch in text if not ch.isspace() and unicodedata.category(ch)[0] not in "PSZ")


def speech_seconds(line: dict[str, Any]) -> float:
    return speech_chars(line["text"]) / (CHARS_PER_SECOND * line["delivery"]["speed"])


class _Semantic:
    def __init__(self, doc: dict[str, Any], report: Report):
        self.doc = doc
        self.report = report
        self.ids: dict[str, str] = {}  # id → 首次出现的路径

    def error(self, path: str, message: str) -> None:
        self.report.errors.append(Issue(path, message))

    def warn(self, path: str, message: str) -> None:
        self.report.warnings.append(Issue(path, message))

    def run(self) -> None:
        doc = self.doc
        self._blank_strings(doc, "$")
        if not IR_VERSION_PATTERN.match(doc["ir_version"]):
            self.error("$.ir_version", f"v0 文档的 ir_version 应为 0.x.y，实际为 {doc['ir_version']!r}")
        self._id(doc["series"]["id"], "$.series.id")

        characters = {}
        if not doc["characters"]:
            self.error("$.characters", "至少需要 1 个角色")
        for i, ch in enumerate(doc["characters"]):
            path = f"$.characters[{i}]"
            self._id(ch["id"], f"{path}.id")
            characters[ch["id"]] = ch
            if not 0 < ch["age"] <= 120:
                self.error(f"{path}.age", f"年龄应在 1–120 之间，实际为 {ch['age']}")

        appeared: set[str] = set()
        if not doc["episodes"]:
            self.error("$.episodes", "至少需要 1 集")
        numbers: dict[int, str] = {}
        for e, ep in enumerate(doc["episodes"]):
            epath = f"$.episodes[{e}]"
            self._id(ep["id"], f"{epath}.id")
            if ep["number"] < 1:
                self.error(f"{epath}.number", f"集数应从 1 开始，实际为 {ep['number']}")
            elif ep["number"] in numbers:
                self.error(f"{epath}.number", f"集数 {ep['number']} 与 {numbers[ep['number']]} 重复")
            else:
                numbers[ep["number"]] = f"{epath}.number"
            if ep["target_duration_s"] <= 0:
                self.error(f"{epath}.target_duration_s", "目标时长必须大于 0")
            if not ep["scenes"]:
                self.error(f"{epath}.scenes", "至少需要 1 场")
            total = 0.0
            for s, sc in enumerate(ep["scenes"]):
                spath = f"{epath}.scenes[{s}]"
                self._id(sc["scene_id"], f"{spath}.scene_id")
                if not sc["shots"]:
                    self.error(f"{spath}.shots", "至少需要 1 个镜头")
                for h, shot in enumerate(sc["shots"]):
                    total += self._shot(shot, f"{spath}.shots[{h}]", characters, appeared)
            target = ep["target_duration_s"]
            if target > 0 and abs(total - target) > target * DURATION_TOLERANCE:
                self.warn(
                    f"{epath}.target_duration_s",
                    f"镜头时长合计 {total:g} 秒，与目标 {target:g} 秒偏差超过 {DURATION_TOLERANCE:.0%}",
                )

        for i, ch in enumerate(doc["characters"]):
            if ch["id"] not in appeared:
                self.warn(f"$.characters[{i}]", f"角色 '{ch['id']}' 从未出镜，也没有台词")

    def _shot(self, shot: dict[str, Any], path: str, characters: dict, appeared: set[str]) -> float:
        self._id(shot["shot_id"], f"{path}.shot_id")
        hint = shot["duration"]["hint_s"]
        if not 0 < hint <= MAX_SHOT_S:
            self.error(f"{path}.duration.hint_s", f"镜头时长应在 (0, {MAX_SHOT_S:g}] 秒内，实际为 {hint:g}")

        on_screen: set[str] = set()
        for c, sc in enumerate(shot["characters"]):
            cpath = f"{path}.characters[{c}].character_id"
            cid = sc["character_id"]
            if cid not in characters:
                self.error(cpath, f"引用了不存在的角色 '{cid}'")
            elif cid in on_screen:
                self.error(cpath, f"角色 '{cid}' 在同一镜头中重复出现")
            on_screen.add(cid)
            appeared.add(cid)

        speech = 0.0
        for d, line in enumerate(shot["dialogue"]):
            lpath = f"{path}.dialogue[{d}]"
            self._id(line["line_id"], f"{lpath}.line_id")
            speaker = line["speaker"]
            appeared.add(speaker)
            if speaker not in characters:
                self.error(f"{lpath}.speaker", f"引用了不存在的角色 '{speaker}'")
            elif line["kind"] == "dialogue" and speaker not in on_screen:
                self.warn(f"{lpath}.speaker", f"'{speaker}' 的对白需要口型，但不在本镜头画面中；画外音请用 kind=voiceover")
            delivery = line["delivery"]
            if not 0 <= delivery["intensity"] <= 1:
                self.error(f"{lpath}.delivery.intensity", f"情绪强度应在 0–1 之间，实际为 {delivery['intensity']:g}")
            lo, hi = SPEED_RANGE
            if not lo <= delivery["speed"] <= hi:
                self.error(f"{lpath}.delivery.speed", f"语速倍率应在 {lo:g}–{hi:g} 之间，实际为 {delivery['speed']:g}")
            else:
                speech += speech_seconds(line)
        if 0 < hint and speech > hint:
            self.warn(
                f"{path}.duration.hint_s",
                f"台词估算朗读 {speech:.1f} 秒（每秒 {CHARS_PER_SECOND:g} 字），超过镜头时长 {hint:g} 秒",
            )
        return hint

    def _id(self, value: str, path: str) -> None:
        if not ID_PATTERN.match(value):
            self.error(path, f"id {value!r} 格式不合法：应匹配 {ID_PATTERN.pattern}")
        if value in self.ids:
            self.error(path, f"id '{value}' 与 {self.ids[value]} 重复")
        else:
            self.ids[value] = path

    def _blank_strings(self, value: Any, path: str) -> None:
        if isinstance(value, str):
            if not value.strip():
                self.error(path, "文本不能为空")
        elif isinstance(value, dict):
            for k, v in value.items():
                self._blank_strings(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self._blank_strings(v, f"{path}[{i}]")
