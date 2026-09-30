"""解析开发进度状态：docs/roadmap.md（计划层）与 docs/progress.md（执行层）。

这是进度格式的唯一解析实现，被以下代码共用：
  - tools/workflow/hooks.py        会话开始注入进度、停止前防丢失、/progress 摘要
  - tools/archcheck/archcheck.py   progress 规则（状态一致性检查）

只依赖标准库。
"""

from __future__ import annotations

import dataclasses
import re

STEP_ID_RE = re.compile(r"^P\d-\d{2}$")
MILESTONE_HEADING_RE = re.compile(r"^###\s+(M\d+\.\d+)\s+(.*)$")
MILESTONE_CELL_RE = re.compile(r"^(M\d+\.\d+)\s*(.*)$")
FIELD_RE = re.compile(r"^\s*-\s*\*\*(.+?)\*\*\s*[:：]\s*(.*?)\s*$")
CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$")

STATUSES = {
    "⬜": "未开始",
    "🔄": "进行中",
    "✅": "完成",
    "⏸": "暂停",
}
DONE, DOING, TODO = "✅", "🔄", "⬜"

SETTINGS_HEADING = "自主推进设置"
PENDING_OPEN = "待决"


@dataclasses.dataclass
class Step:
    id: str
    title: str
    status: str
    milestone: str
    milestone_title: str
    deps: list[str]
    order: int


@dataclasses.dataclass
class Progress:
    fields: dict[str, str]
    has_settings: bool
    subtasks: list[tuple[bool, str]]
    pending: list[dict[str, str]]

    @property
    def current_step(self) -> str | None:
        value = self.fields.get("当前步骤", "")
        m = re.match(r"(P\d-\d{2})", value)
        return m.group(1) if m else None


# ---------------------------------------------------------------------------
# roadmap.md
# ---------------------------------------------------------------------------

def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _expand_deps(cell: str, known: list[str]) -> list[str]:
    """把 'P0-03 ~ P0-11'、'P0-04、P0-06'、'—' 解析为步骤列表。"""
    deps: list[str] = []
    for part in re.split(r"[、,，]", cell):
        part = part.strip()
        rng = re.match(r"(P\d-\d{2})\s*[~～-]\s*(P\d-\d{2})$", part)
        if rng and rng.group(1) in known and rng.group(2) in known:
            a, b = known.index(rng.group(1)), known.index(rng.group(2))
            deps.extend(known[a:b + 1])
            continue
        deps.extend(re.findall(r"P\d-\d{2}", part))
    return deps


def parse_roadmap(text: str) -> dict[str, Step]:
    """解析步骤表。P0/P1 表：编号|步骤|交付|验收|依赖|估时|状态；P2 表：里程碑|编号|步骤|目标|状态。"""
    steps: dict[str, Step] = {}
    raw_deps: dict[str, str] = {}
    milestone, milestone_title = "", ""
    for line in text.splitlines():
        heading = MILESTONE_HEADING_RE.match(line)
        if heading:
            milestone = heading.group(1)
            milestone_title = re.sub(r"[（(].*?[)）]|★", "", heading.group(2)).strip()
            continue
        if not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        idx = next((i for i, c in enumerate(cells[:2]) if STEP_ID_RE.match(c)), None)
        if idx is None or cells[-1] not in STATUSES:
            continue
        if idx == 1:  # P2 表：第一列是里程碑，空白表示沿用上一行
            m = MILESTONE_CELL_RE.match(cells[0])
            if m:
                milestone, milestone_title = m.group(1), m.group(2).strip()
        step_id = cells[idx]
        steps[step_id] = Step(
            id=step_id,
            title=cells[idx + 1] if idx + 1 < len(cells) else "",
            status=cells[-1],
            milestone=milestone,
            milestone_title=milestone_title,
            deps=[],
            order=len(steps),
        )
        if idx == 0 and len(cells) >= 7:
            raw_deps[step_id] = cells[4]
    known = list(steps)
    for step_id, cell in raw_deps.items():
        steps[step_id].deps = _expand_deps(cell, known)
    return steps


def in_progress(steps: dict[str, Step]) -> list[Step]:
    return [s for s in steps.values() if s.status == DOING]


def next_ready_step(steps: dict[str, Step]) -> Step | None:
    """最早未完成阶段内，编号顺序上第一个未开始、且依赖全部完成的步骤。

    只在最早的未完成阶段中挑选：P2 以后的表没有依赖列，不能越过前一阶段提前开工。
    """
    ordered = sorted(steps.values(), key=lambda s: s.order)
    phase = next((s.id[:2] for s in ordered if s.status != DONE), None)
    for step in ordered:
        if step.id[:2] != phase:
            continue
        if step.status == TODO and all(steps.get(d) and steps[d].status == DONE for d in step.deps):
            return step
    return None


# ---------------------------------------------------------------------------
# progress.md
# ---------------------------------------------------------------------------

def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    return sections


def _table(lines: list[str]) -> list[dict[str, str]]:
    rows = [line for line in lines if line.lstrip().startswith("|")]
    if len(rows) < 2:
        return []
    header = _cells(rows[0])
    out = []
    for row in rows[2:]:
        cells = _cells(row)
        if any(cells):
            out.append(dict(zip(header, cells)))
    return out


def parse_progress(text: str) -> Progress:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        m = FIELD_RE.match(line)
        if m:
            fields.setdefault(m.group(1), m.group(2))
    sections = _sections(text)
    subtasks = [
        (m.group(1).lower() == "x", m.group(2))
        for line in sections.get("子任务", [])
        if (m := CHECKBOX_RE.match(line))
    ]
    pending = [row for row in _table(sections.get("待决事项", [])) if row.get("状态") == PENDING_OPEN]
    return Progress(
        fields=fields,
        has_settings=SETTINGS_HEADING in sections,
        subtasks=subtasks,
        pending=pending,
    )


# ---------------------------------------------------------------------------
# 一致性检查（archcheck 的 progress 规则调用）
# ---------------------------------------------------------------------------

def consistency_problems(steps: dict[str, Step], progress: Progress, max_in_progress: int) -> list[str]:
    problems: list[str] = []
    doing = in_progress(steps)
    if len(doing) > max_in_progress:
        problems.append(
            f"路线图中同时进行的步骤有 {len(doing)} 个（{', '.join(s.id for s in doing)}），上限为 {max_in_progress}")
    if not progress.has_settings:
        problems.append(f"缺少“## {SETTINGS_HEADING}”段落")
    current = progress.current_step
    if current is None:
        problems.append("缺少“- **当前步骤**：Px-yy”字段")
    elif current not in steps:
        problems.append(f"当前步骤 {current} 不在路线图中")
    else:
        if steps[current].status == DONE:
            problems.append(f"当前步骤 {current} 在路线图中已完成，应指向下一个步骤")
        if doing and current not in {s.id for s in doing}:
            problems.append(
                f"当前步骤 {current} 不在路线图的进行中步骤（{', '.join(s.id for s in doing)}）之中")
    return problems
