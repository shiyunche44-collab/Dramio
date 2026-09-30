import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from dramio_drama_ir.__main__ import main
from dramio_drama_ir.render import render_markdown

from .helpers import PACKAGE_DIR, SAMPLE, SAMPLE_MD, sample

try:
    import jsonschema  # 可选：只用于差分测试，不是依赖
except ImportError:  # pragma: no cover
    jsonschema = None


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, doc, name="doc.json") -> str:
        path = Path(self.tmp.name) / name
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_valid_sample(self):
        code, out, _ = run("validate", "--strict", str(SAMPLE))
        self.assertEqual(code, 0)
        self.assertIn("通过（0 个错误，0 个警告）", out)

    def test_invalid_document_reports_path(self):
        doc = sample()
        doc["episodes"][0]["scenes"][1]["shots"][2]["dialogue"][0]["speaker"] = "char_x"
        path = self.write(doc)
        code, out, _ = run("validate", path)
        self.assertEqual(code, 1)
        self.assertIn(f"{path}: 错误 $.episodes[0].scenes[1].shots[2].dialogue[0].speaker: 引用了不存在的角色 'char_x'", out)

    def test_strict_fails_on_warning(self):
        doc = sample()
        doc["episodes"][0]["target_duration_s"] = 90
        path = self.write(doc)
        self.assertEqual(run("validate", path)[0], 0)
        self.assertEqual(run("validate", "--strict", path)[0], 1)

    def test_json_output(self):
        doc = sample()
        doc["characters"][0]["nickname"] = "x"
        code, out, _ = run("validate", "--json", str(SAMPLE), self.write(doc))
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual([r["ok"] for r in data], [True, False])
        self.assertEqual(data[1]["errors"][0]["path"], "$.characters[0].nickname")

    def test_bad_json_is_invalid(self):
        code, out, _ = run("validate", self.write("{\"ir_version\": "))
        self.assertEqual(code, 1)
        self.assertIn("不是合法的 JSON", out)

    def test_missing_file_is_usage_error(self):
        code, _, err = run("validate", str(Path(self.tmp.name) / "nope.json"))
        self.assertEqual(code, 2)
        self.assertIn("无法读取", err)

    def test_render_refuses_invalid(self):
        doc = sample()
        del doc["series"]["title"]
        code, out, err = run("render", self.write(doc))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("title", err)

    def test_module_entrypoint(self):
        env = dict(os.environ, PYTHONPATH=str(PACKAGE_DIR / "python"))
        proc = subprocess.run(
            [sys.executable, "-m", "dramio_drama_ir", "validate", "--strict", str(SAMPLE)],
            capture_output=True, text=True, env=env, cwd=tempfile.gettempdir(),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class RenderTest(unittest.TestCase):
    def test_committed_markdown_is_up_to_date(self):
        expected = render_markdown(sample())
        self.assertEqual(
            SAMPLE_MD.read_text(encoding="utf-8"), expected,
            "ep01.md 与 render 输出不一致；运行 python3 -m dramio_drama_ir render ... > ep01.md 重新生成",
        )

    def test_render_mentions_every_line(self):
        md = render_markdown(sample())
        for shot in (s for sc in sample()["episodes"][0]["scenes"] for s in sc["shots"]):
            for line in shot["dialogue"]:
                self.assertIn(line["text"], md)


@unittest.skipUnless(jsonschema, "未安装 jsonschema，跳过差分测试")
class DifferentialTest(unittest.TestCase):
    """装了 jsonschema 时，用它交叉校验结构类结论（它不是依赖）。"""

    def test_structural_verdicts_agree(self):
        from dramio_drama_ir import load_schema
        from dramio_drama_ir.schema import structural_issues

        schema = load_schema()
        docs = [sample()]
        d = sample(); del d["series"]["title"]; docs.append(d)
        d = sample(); d["characters"][0]["x"] = 1; docs.append(d)
        d = sample(); d["episodes"][0]["scenes"][0]["mood"] = "angry"; docs.append(d)
        d = sample(); d["characters"][0]["age"] = "26"; docs.append(d)
        validator = jsonschema.Draft202012Validator(schema)
        for doc in docs:
            self.assertEqual(not structural_issues(doc, schema), validator.is_valid(doc))
