"""工作流进度解析与 hooks 的单元测试。运行：python3 -m unittest discover -s tools/workflow -p "test_*.py" """

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import progress_state as ps

HERE = Path(__file__).resolve().parent
HOOKS = HERE / "hooks.py"
REPO_ROOT = HERE.parent.parent

ROADMAP = textwrap.dedent("""\
    ### M0.1 准备（约 1.5 天）

    | 编号 | 步骤 | 交付（可演示） | 验收标准 | 依赖 | 估时 | 状态 |
    |---|---|---|---|---|---|---|
    | P0-01 | 脚手架 | x | x | — | 0.5 天 | {s1} |
    | P0-02 | 样例 | x | x | — | 1 天 | {s2} |

    ### M0.2 摸底（约 2 周）★

    | 编号 | 步骤 | 交付（可演示） | 验收标准 | 依赖 | 估时 | 状态 |
    |---|---|---|---|---|---|---|
    | P0-03 | 剧本 | x | x | P0-02 | 1.5 天 | {s3} |
    | P0-04 | 串联 | x | x | P0-01 ~ P0-03 | 2 天 | {s4} |

    | 里程碑 | 编号 | 步骤 | 目标 | 状态 |
    |---|---|---|---|---|
    | M2.1 质量 | P2-01 | 评审 | x | ⬜ |
    |  | P2-02 | 择优 | x | ⬜ |
    """)

PROGRESS = textwrap.dedent("""\
    # 开发进度

    ## 自主推进设置

    | 项 | 设置 |
    |---|---|

    ## 当前状态

    - **当前里程碑**：M0.1 准备
    - **当前步骤**：{current}
    - **步骤状态**：进行中

    ## 子任务

    - [x] 建项目
    - [ ] 写 doctor 命令

    ## 本步费用

    - **已花费**：¥12
    - **上限**：¥100

    ## 待决事项

    | 编号 | 步骤 | 问题 | 候选 | 推荐与理由 | 状态 | 决定 |
    |---|---|---|---|---|---|---|
    | D-001 | P0-02 | 选哪套定妆 | A/B/C | B：五官最稳定 | 待决 | |
    | D-002 | P0-01 | 用哪家 | X/Y | X | 已决 | X |
    """)


def roadmap(s1="⬜", s2="⬜", s3="⬜", s4="⬜") -> str:
    return ROADMAP.format(s1=s1, s2=s2, s3=s3, s4=s4)


class RoadmapTest(unittest.TestCase):
    def test_parses_steps_milestones_and_deps(self) -> None:
        steps = ps.parse_roadmap(roadmap())
        self.assertEqual(list(steps), ["P0-01", "P0-02", "P0-03", "P0-04", "P2-01", "P2-02"])
        self.assertEqual(steps["P0-03"].milestone, "M0.2")
        self.assertEqual(steps["P0-03"].milestone_title, "摸底")
        self.assertEqual(steps["P0-04"].deps, ["P0-01", "P0-02", "P0-03"])
        self.assertEqual(steps["P0-01"].deps, [])
        self.assertEqual(steps["P2-02"].milestone, "M2.1")  # 空白里程碑列沿用上一行

    def test_next_ready_step_respects_deps(self) -> None:
        self.assertEqual(ps.next_ready_step(ps.parse_roadmap(roadmap())).id, "P0-01")
        # P0-03、P0-04 依赖未完成的 P0-02；也不能越过 P0 提前开始 P2
        self.assertIsNone(ps.next_ready_step(ps.parse_roadmap(roadmap(s1="✅", s2="🔄"))))
        steps = ps.parse_roadmap(roadmap(s1="✅", s2="✅"))
        self.assertEqual(ps.next_ready_step(steps).id, "P0-03")

    def test_real_roadmap_parses(self) -> None:
        steps = ps.parse_roadmap((REPO_ROOT / "docs/roadmap.md").read_text(encoding="utf-8"))
        self.assertEqual(sum(1 for s in steps if s.startswith("P0")), 14)
        self.assertEqual(sum(1 for s in steps if s.startswith("P1")), 30)
        self.assertIn("P0-11", steps["P0-12"].deps)


class ProgressTest(unittest.TestCase):
    def test_parses_fields_subtasks_and_open_pending(self) -> None:
        p = ps.parse_progress(PROGRESS.format(current="P0-02"))
        self.assertTrue(p.has_settings)
        self.assertEqual(p.current_step, "P0-02")
        self.assertEqual(p.fields["已花费"], "¥12")
        self.assertEqual(p.subtasks, [(True, "建项目"), (False, "写 doctor 命令")])
        self.assertEqual([row["编号"] for row in p.pending], ["D-001"])

    def test_consistency_ok(self) -> None:
        steps = ps.parse_roadmap(roadmap(s1="✅", s2="🔄"))
        p = ps.parse_progress(PROGRESS.format(current="P0-02"))
        self.assertEqual(ps.consistency_problems(steps, p, 2), [])

    def test_too_many_in_progress(self) -> None:
        steps = ps.parse_roadmap(roadmap(s1="🔄", s2="🔄", s3="🔄"))
        p = ps.parse_progress(PROGRESS.format(current="P0-02"))
        self.assertEqual(len(ps.consistency_problems(steps, p, 2)), 1)

    def test_current_step_done(self) -> None:
        steps = ps.parse_roadmap(roadmap(s1="✅"))
        p = ps.parse_progress(PROGRESS.format(current="P0-01"))
        self.assertEqual(len(ps.consistency_problems(steps, p, 2)), 1)

    def test_current_step_unknown_or_missing(self) -> None:
        steps = ps.parse_roadmap(roadmap())
        self.assertEqual(len(ps.consistency_problems(steps, ps.parse_progress(PROGRESS.format(current="P9-99")), 2)), 1)
        self.assertEqual(len(ps.consistency_problems(steps, ps.parse_progress(PROGRESS.format(current="无")), 2)), 1)

    def test_current_step_not_among_in_progress(self) -> None:
        steps = ps.parse_roadmap(roadmap(s1="🔄"))
        p = ps.parse_progress(PROGRESS.format(current="P0-02"))
        self.assertEqual(len(ps.consistency_problems(steps, p, 2)), 1)

    def test_missing_settings(self) -> None:
        steps = ps.parse_roadmap(roadmap())
        p = ps.parse_progress(PROGRESS.format(current="P0-01").replace("## 自主推进设置", "## 其他"))
        self.assertEqual(len(ps.consistency_problems(steps, p, 2)), 1)


class HooksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "docs").mkdir()
        self.write_state(roadmap(), PROGRESS.format(current="P0-01"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_state(self, roadmap_text: str, progress_text: str) -> None:
        (self.root / "docs/roadmap.md").write_text(roadmap_text, encoding="utf-8")
        (self.root / "docs/progress.md").write_text(progress_text, encoding="utf-8")

    def hook(self, command: str, payload: dict) -> subprocess.CompletedProcess:
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)}
        return subprocess.run([sys.executable, str(HOOKS), command], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def test_session_start_injects_context(self) -> None:
        result = self.hook("session-start", {"hook_event_name": "SessionStart", "source": "startup"})
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("P0-01 脚手架", context)
        self.assertIn("D-001", context)
        self.assertIn("断点：写 doctor 命令", context)
        self.assertIn("/continue", context)

    def test_session_start_without_state_is_silent(self) -> None:
        (self.root / "docs/progress.md").unlink()
        result = self.hook("session-start", {})
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_stop_guard_blocks_dirty_step_in_progress(self) -> None:
        self.write_state(roadmap(s1="🔄"), PROGRESS.format(current="P0-01"))
        self.git("init", "-q")
        (self.root / "work.txt").write_text("wip", encoding="utf-8")
        result = self.hook("stop-guard", {"hook_event_name": "Stop", "stop_hook_active": False})
        self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_stop_guard_allows_when_already_active(self) -> None:
        self.write_state(roadmap(s1="🔄"), PROGRESS.format(current="P0-01"))
        self.git("init", "-q")
        (self.root / "work.txt").write_text("wip", encoding="utf-8")
        result = self.hook("stop-guard", {"hook_event_name": "Stop", "stop_hook_active": True})
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_stop_guard_allows_when_no_step_in_progress(self) -> None:
        self.git("init", "-q")
        (self.root / "work.txt").write_text("wip", encoding="utf-8")
        result = self.hook("stop-guard", {"hook_event_name": "Stop", "stop_hook_active": False})
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_stop_guard_allows_clean_tree(self) -> None:
        self.write_state(roadmap(s1="🔄"), PROGRESS.format(current="P0-01"))
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        result = self.hook("stop-guard", {"hook_event_name": "Stop", "stop_hook_active": False})
        self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
