#!/usr/bin/env python3
"""Claude Code 工作流 hooks 与进度摘要（配置见 .claude/settings.json）。

子命令：
  session-start  SessionStart hook：把当前进度注入 Claude 的上下文
  stop-guard     Stop hook：步骤进行中且有未提交改动时，阻止停止，要求先存档
  summary        打印进度摘要（/progress 使用）

只依赖标准库。任何解析失败都放行（exit 0），hook 不能妨碍正常使用。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import progress_state as ps  # noqa: E402

ROADMAP = "docs/roadmap.md"
PROGRESS = "docs/progress.md"
COMMANDS = "继续推进：/continue（或说“继续当前进度”） · 单步：/step · 查看：/progress · 拍板：/decide"


def project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def load(root: Path) -> tuple[dict[str, ps.Step], ps.Progress] | None:
    try:
        roadmap = (root / ROADMAP).read_text(encoding="utf-8")
        progress = (root / PROGRESS).read_text(encoding="utf-8")
    except OSError:
        return None
    return ps.parse_roadmap(roadmap), ps.parse_progress(progress)


def read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except (ValueError, OSError):
        return {}


def summary(root: Path) -> str | None:
    loaded = load(root)
    if loaded is None:
        return None
    steps, progress = loaded
    lines = ["【Dramio 开发进度】来源：docs/progress.md、docs/roadmap.md"]

    current = progress.current_step
    step = steps.get(current) if current else None
    if step:
        lines.append(f"当前里程碑：{step.milestone} {step.milestone_title}")
        lines.append(
            f"当前步骤：{step.id} {step.title}（路线图 {step.status} {ps.STATUSES[step.status]}；"
            f"进度文件：{progress.fields.get('步骤状态', '—')}）")
    else:
        lines.append(f"当前步骤：{current or '未设置'}")

    for key in ("工作分支", "PR"):
        value = progress.fields.get(key)
        if value and value not in ("—", "-"):
            lines.append(f"{key}：{value}")

    if progress.subtasks:
        done = sum(1 for ok, _ in progress.subtasks if ok)
        nxt = next((t for ok, t in progress.subtasks if not ok), None)
        tail = f"；断点：{nxt}" if nxt else ""
        lines.append(f"子任务：{done}/{len(progress.subtasks)} 完成{tail}")

    spent, cap = progress.fields.get("已花费"), progress.fields.get("上限")
    if spent and cap:
        lines.append(f"本步费用：{spent} / {cap}")

    if progress.pending:
        lines.append(f"待用户决定（{len(progress.pending)} 项）：")
        for row in progress.pending:
            lines.append(f"  - {row.get('编号', '?')}（{row.get('步骤', '?')}）{row.get('问题', '')}"
                         f"；推荐：{row.get('推荐与理由', '—')}")
    else:
        lines.append("待用户决定：无")

    doing = ps.in_progress(steps)
    if doing:
        lines.append("进行中：" + "、".join(f"{s.id} {s.title}" for s in doing))
    nxt_step = ps.next_ready_step(steps)
    if nxt_step:
        lines.append(f"下一个可开始的步骤：{nxt_step.id} {nxt_step.title}（{nxt_step.milestone}）")

    phases: dict[str, list[ps.Step]] = {}
    for s in steps.values():
        phases.setdefault(s.id[:2], []).append(s)
    lines.append("总体：" + "，".join(
        f"{p} {sum(1 for s in ss if s.status == ps.DONE)}/{len(ss)}" for p, ss in phases.items()))
    lines.append(COMMANDS)
    return "\n".join(lines)


def session_start(root: Path) -> int:
    read_stdin_json()
    text = summary(root)
    if text is None:
        return 0
    context = (
        text
        + "\n\n当用户说“继续当前进度”“继续”“接着做”时，执行 /continue"
          "（.claude/skills/continue/SKILL.md），不要重新询问背景。"
    )
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context},
    }, ensure_ascii=False))
    return 0


def git_dirty(root: Path) -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(out.strip())


def stop_guard(root: Path) -> int:
    payload = read_stdin_json()
    if payload.get("stop_hook_active"):
        return 0
    loaded = load(root)
    if loaded is None:
        return 0
    steps, _ = loaded
    doing = ps.in_progress(steps)
    if not doing or not git_dirty(root):
        return 0
    ids = "、".join(s.id for s in doing)
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"步骤 {ids} 正在进行中，工作区还有未提交的改动。停止前请先存档："
            "在 docs/progress.md 勾选已完成的子任务、追加交接日志（做了什么、下一步），"
            "然后提交并推送。云端容器随时可能被回收，未推送的进度会丢失。"
        ),
    }, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("session-start", "stop-guard", "summary"):
        print("用法：hooks.py session-start|stop-guard|summary", file=sys.stderr)
        return 1
    root = project_root()
    if argv[1] == "session-start":
        return session_start(root)
    if argv[1] == "stop-guard":
        return stop_guard(root)
    text = summary(root)
    print(text if text is not None else "未找到 docs/progress.md 或 docs/roadmap.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
