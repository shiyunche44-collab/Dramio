"""archcheck 的单元测试。运行：python3 -m unittest discover -s tools/archcheck -p "test_*.py" """

from __future__ import annotations

import datetime as dt
import tempfile
import textwrap
import unittest
from pathlib import Path

import archcheck

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
RULES = HERE / "rules.toml"
TODAY = dt.date(2026, 10, 1)

VALID_ADR = textwrap.dedent("""\
    # ADR-0001：示例决策

    - **状态**：Accepted
    - **日期**：2026-09-30
    """)
VALID_INDEX = "| [ADR-0001](0001-example.md) | 示例决策 | Accepted |\n"


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.exceptions = self.root / "exceptions.toml"
        self.write("docs/adr/README.md", VALID_INDEX)
        self.write("docs/adr/0001-example.md", VALID_ADR)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, content: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")

    def run_check(self, today: dt.date = TODAY) -> archcheck.Report:
        return archcheck.run(self.root, RULES, self.exceptions, today)

    def rules_hit(self, report: archcheck.Report) -> list[str]:
        return sorted(v.rule for v in report.violations)


class LayoutTest(RepoCase):
    def test_known_layout_passes(self) -> None:
        self.write("apps/web/src/page.tsx", "export default function Page() {}\n")
        self.write("packages/drama-ir/index.ts", "export const x = 1;\n")
        self.assertEqual(self.rules_hit(self.run_check()), [])

    def test_unknown_top_level_dir_fails(self) -> None:
        self.write("misc/notes.txt", "hello\n")
        self.assertEqual(self.rules_hit(self.run_check()), ["layout"])


class VendorSdkTest(RepoCase):
    def test_python_vendor_sdk_outside_gateway_fails(self) -> None:
        self.write("workers/llm/writer.py", "import anthropic\n")
        report = self.run_check()
        self.assertEqual(self.rules_hit(report), ["vendor-sdk"])
        self.assertEqual(report.violations[0].line, 1)

    def test_python_submodule_import_fails(self) -> None:
        self.write("workers/llm/writer.py", "x = 1\nfrom openai.types import Model\n")
        report = self.run_check()
        self.assertEqual(self.rules_hit(report), ["vendor-sdk"])
        self.assertEqual(report.violations[0].line, 2)

    def test_node_vendor_sdk_outside_gateway_fails(self) -> None:
        self.write("apps/api/src/script.ts", "import Anthropic from '@anthropic-ai/sdk';\n")
        self.assertEqual(self.rules_hit(self.run_check()), ["vendor-sdk"])

    def test_vendor_sdk_inside_gateway_passes(self) -> None:
        self.write("services/model-gateway/adapters/claude.py", "import anthropic\n")
        self.write("services/model-gateway/src/openai.ts", "import OpenAI from 'openai';\n")
        self.assertEqual(self.rules_hit(self.run_check()), [])

    def test_similar_names_do_not_match(self) -> None:
        self.write("workers/llm/util.py", "import openai_compat_helpers\nimport anthropics_notes\n")
        self.assertEqual(self.rules_hit(self.run_check()), [])


class ModelIdTest(RepoCase):
    def test_hardcoded_model_id_fails(self) -> None:
        self.write("workers/llm/writer.py", 'MODEL = "claude-opus-5-5"\n')
        self.assertEqual(self.rules_hit(self.run_check()), ["model-id"])

    def test_model_id_in_gateway_passes(self) -> None:
        self.write("services/model-gateway/routes.ts", "const m = 'claude-sonnet-5-5';\n")
        self.assertEqual(self.rules_hit(self.run_check()), [])

    def test_code_outside_zones_is_not_scanned(self) -> None:
        self.write("tools/bench/run.py", 'MODEL = "gpt-5"\n')
        self.assertEqual(self.rules_hit(self.run_check()), [])


class DependencyTest(RepoCase):
    def test_app_importing_service_via_relative_path_fails(self) -> None:
        self.write("apps/web/src/a.ts", "import { wf } from '../../../services/orchestrator/src/wf';\n")
        self.assertEqual(self.rules_hit(self.run_check()), ["dependency"])

    def test_app_importing_service_via_alias_fails(self) -> None:
        self.write("apps/web/src/a.ts", "import { wf } from '@dramio/orchestrator/workflows';\n")
        self.assertEqual(self.rules_hit(self.run_check()), ["dependency"])

    def test_importing_shared_package_passes(self) -> None:
        self.write("apps/web/src/a.ts", "import { Shot } from '@dramio/drama-ir';\n")
        self.write("workers/render/a.py", "from dramio_ir.shot import Shot\n")
        self.assertEqual(self.rules_hit(self.run_check()), [])

    def test_alias_boundary_is_respected(self) -> None:
        # '@dramio/web-kit' 是共享包，不应被当成 '@dramio/web' 应用
        self.write("workers/render/a.ts", "import { x } from '@dramio/web-kit';\n")
        self.assertEqual(self.rules_hit(self.run_check()), [])

    def test_package_depending_on_app_fails(self) -> None:
        self.write("packages/ui/index.ts", "import { api } from '@dramio/api';\n")
        self.assertEqual(self.rules_hit(self.run_check()), ["dependency"])

    def test_spikes_cannot_be_imported(self) -> None:
        self.write("workers/video/a.py", "from ...spikes.poc import run\n")
        self.assertEqual(self.rules_hit(self.run_check()), ["dependency"])

    def test_spikes_poc_package_cannot_be_imported(self) -> None:
        # 经 PYTHONPATH=spikes/poc 引用 poc 包，同样是依赖 spikes（D-001）
        self.write("workers/llm/a.py", "from poc.runlog import Run\n")
        self.write("apps/api/b.py", "import poc\n")
        self.assertEqual(self.rules_hit(self.run_check()), ["dependency", "dependency"])

    def test_poc_imports_inside_spikes_and_similar_names_pass(self) -> None:
        self.write("spikes/poc/poc/doctor.py", "from poc import config\n")
        self.write("workers/llm/a.py", "import pocket\nfrom pocketbase import x\n")
        self.assertEqual(self.rules_hit(self.run_check()), [])

    def test_same_zone_relative_import_passes(self) -> None:
        self.write("workers/video/a.py", "from .helpers import clip\nfrom ..common import io\n")
        self.assertEqual(self.rules_hit(self.run_check()), [])


class AdrTest(RepoCase):
    def test_missing_index_fails(self) -> None:
        (self.root / "docs/adr/README.md").unlink()
        self.assertIn("adr", self.rules_hit(self.run_check()))

    def test_adr_not_in_index_fails(self) -> None:
        self.write("docs/adr/0002-second.md", VALID_ADR.replace("0001", "0002"))
        self.assertEqual(self.rules_hit(self.run_check()), ["adr"])

    def test_invalid_status_fails(self) -> None:
        self.write("docs/adr/0001-example.md", VALID_ADR.replace("Accepted", "Done"))
        self.assertEqual(self.rules_hit(self.run_check()), ["adr"])

    def test_missing_status_fails(self) -> None:
        self.write("docs/adr/0001-example.md", "# ADR-0001：示例\n")
        self.assertEqual(self.rules_hit(self.run_check()), ["adr"])

    def test_title_number_mismatch_fails(self) -> None:
        self.write("docs/adr/0001-example.md", VALID_ADR.replace("# ADR-0001", "# ADR-0007"))
        self.assertEqual(self.rules_hit(self.run_check()), ["adr"])

    def test_superseded_requires_reference(self) -> None:
        self.write("docs/adr/0001-example.md", VALID_ADR.replace("Accepted", "Superseded"))
        self.assertEqual(self.rules_hit(self.run_check()), ["adr"])
        self.write("docs/adr/0001-example.md",
                   VALID_ADR.replace("Accepted", "Superseded") + "\n被 ADR-0005 取代。\n")
        self.assertEqual(self.rules_hit(self.run_check()), [])

    def test_bad_filename_fails(self) -> None:
        self.write("docs/adr/Some_Decision.md", "# x\n")
        self.assertEqual(self.rules_hit(self.run_check()), ["adr"])


class ExceptionTest(RepoCase):
    EXC = """\
        [[exception]]
        rule = "vendor-sdk"
        path = "workers/llm/*.py"
        reason = "P1 临时直连"
        owner = "@owner"
        expires = 2026-12-31
        """

    def test_active_exception_suppresses(self) -> None:
        self.write("workers/llm/writer.py", "import anthropic\n")
        self.write("exceptions.toml", self.EXC)
        report = self.run_check()
        self.assertEqual(self.rules_hit(report), [])
        self.assertEqual(report.suppressed, 1)

    def test_expired_exception_fails(self) -> None:
        self.write("workers/llm/writer.py", "import anthropic\n")
        self.write("exceptions.toml", self.EXC)
        report = self.run_check(today=dt.date(2027, 1, 1))
        self.assertEqual(self.rules_hit(report), ["exception", "vendor-sdk"])

    def test_exception_without_owner_fails(self) -> None:
        self.write("exceptions.toml", self.EXC.replace('owner = "@owner"\n', ""))
        self.assertEqual(self.rules_hit(self.run_check()), ["exception"])

    def test_unused_exception_warns(self) -> None:
        self.write("exceptions.toml", self.EXC)
        report = self.run_check()
        self.assertEqual(self.rules_hit(report), [])
        self.assertEqual(len(report.warnings), 1)


class ProgressRuleTest(RepoCase):
    ROADMAP = (
        "| 编号 | 步骤 | 交付 | 验收 | 依赖 | 估时 | 状态 |\n|---|---|---|---|---|---|---|\n"
        "| P0-01 | 脚手架 | x | x | — | 1 天 | {s1} |\n"
        "| P0-02 | 样例 | x | x | — | 1 天 | {s2} |\n"
        "| P0-03 | 剧本 | x | x | — | 1 天 | {s3} |\n"
    )
    PROGRESS = "## 自主推进设置\n\n## 当前状态\n\n- **当前步骤**：{current}\n"

    def state(self, current: str = "P0-01", s1: str = "⬜", s2: str = "⬜", s3: str = "⬜") -> None:
        self.write("docs/roadmap.md", self.ROADMAP.format(s1=s1, s2=s2, s3=s3))
        self.write("docs/progress.md", self.PROGRESS.format(current=current))

    def test_consistent_state_passes(self) -> None:
        self.state(current="P0-02", s1="✅", s2="🔄")
        self.assertEqual(self.rules_hit(self.run_check()), [])

    def test_too_many_in_progress_fails(self) -> None:
        self.state(current="P0-01", s1="🔄", s2="🔄", s3="🔄")
        self.assertEqual(self.rules_hit(self.run_check()), ["progress"])

    def test_current_step_already_done_fails(self) -> None:
        self.state(current="P0-01", s1="✅")
        self.assertEqual(self.rules_hit(self.run_check()), ["progress"])

    def test_missing_progress_file_fails(self) -> None:
        self.write("docs/roadmap.md", self.ROADMAP.format(s1="⬜", s2="⬜", s3="⬜"))
        self.assertEqual(self.rules_hit(self.run_check()), ["progress"])


class RealRepoTest(unittest.TestCase):
    def test_repository_passes(self) -> None:
        report = archcheck.run(REPO_ROOT, RULES, HERE / "exceptions.toml", dt.date.today())
        self.assertEqual([v.format() for v in report.violations], [])


if __name__ == "__main__":
    unittest.main()
