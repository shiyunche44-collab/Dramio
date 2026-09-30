import io
import json
import tempfile
import unittest
from pathlib import Path

from poc import costume, images, seedream
from tests.test_seedream import jpeg

SENTINEL = "ark-SENTINEL-7f3b2c9e1d"
ENV = {"ARK_API_KEY": SENTINEL}


def result(width=1152, height=2048, tag=b"a"):
    img = seedream.GeneratedImage(jpeg(width, height, payload=tag), None, f"{width}x{height}", "jpeg")
    return seedream.ImageResult([img], {"generated_images": 1, "output_tokens": 9216}, "rid", seedream.MODELS["pro"])


class FakeGen:
    """按顺序返回 ImageResult 或抛出 ImageError；None 表示按请求尺寸生成一张唯一的图。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        o = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(o, Exception):
            raise o
        if o is None:
            w, h = (int(x) for x in kw["size"].split("x"))
            return result(w, h, tag=str(len(self.calls)).encode())
        return o


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.doc, _ = costume.load_episode()

    def test_sections_and_comment_lines_dropped(self):
        t = costume.load_templates()
        self.assertEqual(set(t), {"main", "view", "expression", "view_ref", "expression_ref", "view_sheet", "expression_sheet"})
        for tpl in t.values():
            self.assertNotIn(">", tpl.template.splitlines()[0][:1])

    def test_fields_come_from_ir(self):
        jobs = costume.plan_jobs(self.doc, "main", ["char_suwan"], n=3)
        self.assertEqual([j.label for j in jobs], ["01", "02", "03"])
        c = costume.character(self.doc, "char_suwan")
        for field in (c["name"], c["appearance"], c["costume"], self.doc["series"]["visual_style"], "26 岁中国女性"):
            self.assertIn(field, jobs[0].prompt)
        self.assertNotIn("$", jobs[0].prompt)
        self.assertEqual(jobs[0].size, "1152x2048")

    def test_expression_set(self):
        self.assertEqual(costume.expressions_for(self.doc, "char_suwan"), ["neutral", "sad", "angry", "surprised", "sarcastic", "anxious"])
        self.assertEqual(costume.expressions_for(self.doc, "char_luchen"), ["neutral", "sarcastic", "happy", "fearful", "angry"])
        doc = json.loads(json.dumps(self.doc))
        for sc in doc["episodes"][0]["scenes"]:
            for sh in sc["shots"]:
                for i, ln in enumerate(sh.get("dialogue") or []):
                    ln["speaker"] = "char_suwan"
                    ln["delivery"]["emotion"] = list(costume.EXPRESSION_ZH)[i % 10]
        self.assertLessEqual(len(costume.expressions_for(doc, "char_suwan")), costume.MAX_EXPRESSIONS)

    def test_derive_modes(self):
        ref = costume.Ref("x.jpg", "ab" * 32, "jpeg", b"data")
        text = costume.plan_jobs(self.doc, "derive", ["char_luchen"], mode="text")
        self.assertEqual([(j.kind, j.label) for j in text[:3]], [("view", "front"), ("view", "side"), ("view", "back")])
        self.assertEqual(len(text), 3 + 5)
        self.assertTrue(all(not j.refs for j in text))
        refd = costume.plan_jobs(self.doc, "derive", ["char_luchen"], mode="ref", bases={"char_luchen": ref})
        self.assertEqual(len(refd), 8)
        self.assertTrue(all(j.refs == [ref] for j in refd))
        self.assertIn("参考图", refd[0].prompt)
        sheet = costume.plan_jobs(self.doc, "derive", ["char_luchen"], mode="sheet")
        self.assertEqual([(j.label, j.size) for j in sheet], [("views", "2048x1152"), ("expressions", "1536x1536")])
        self.assertIn("5 个表情", sheet[1].prompt)

    def test_small_sizes_meet_minimum_pixels(self):
        for size in costume.SMALL_SIZES.values():
            w, h = (int(x) for x in size.split("x"))
            self.assertGreaterEqual(w * h, 921600)
        jobs = costume.plan_jobs(self.doc, "derive", ["char_suwan"], mode="sheet", sizes=costume.SMALL_SIZES)
        self.assertEqual([j.size for j in jobs], ["1280x720", "960x960"])

    def test_node_key_depends_on_inputs(self):
        job = costume.plan_jobs(self.doc, "main", ["char_suwan"], n=1)[0]
        k = costume.node_key("m", job, "r1")
        self.assertEqual(k, costume.node_key("m", job, "r1"))
        self.assertNotEqual(k, costume.node_key("m", job, "r2"))
        self.assertNotEqual(k, costume.node_key("m2", job, "r1"))
        job.refs = [costume.Ref("x", "cd" * 32, "jpeg")]
        self.assertNotEqual(k, costume.node_key("m", job, "r1"))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_costume(self, settings, gen, env=ENV):
        out = io.StringIO()
        code = costume.run_costume(settings, env=env, base_dir=self.base / "runs", out=out, gen_fn=gen, sleep=lambda s: None)
        run_dir = next((self.base / "runs").iterdir()) if (self.base / "runs").exists() else None
        return code, run_dir, out.getvalue()

    def load(self, run_dir):
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        calls = [json.loads(ln) for ln in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        return summary, calls

    def test_main_stage_success(self):
        gen = FakeGen()
        code, run_dir, _ = self.run_costume(costume.Settings("main", "pro", ["char_suwan"], n=2), gen)
        self.assertEqual(code, 0)
        summary, calls = self.load(run_dir)
        self.assertEqual((summary["ok"], summary["n_expected"], summary["first_ok"]), (2, 2, 2))
        self.assertAlmostEqual(summary["cost_cny_total"], 0.60)
        self.assertAlmostEqual(sum(c["cost_cny"] for c in calls), summary["spent_cny_total"])
        self.assertTrue(all(c["provider"] == "ark" and c["capability"] == "image" and c["cost_basis"] == "estimate" for c in calls))
        self.assertTrue(all(len(c["node_key"]) == 64 for c in calls))
        self.assertFalse(gen.calls[0]["watermark"])
        item = summary["items"][0]
        self.assertTrue(item["t2i_original"] and item["seedance_eligible"])
        data = (run_dir / item["file"]).read_bytes()
        self.assertEqual(images.image_info(data).sha256, item["sha256"])
        raw = (run_dir / "calls.jsonl").read_text(encoding="utf-8") + (run_dir / "summary.json").read_text(encoding="utf-8")
        self.assertNotIn("base64", raw)
        self.assertNotIn(SENTINEL, raw)

    def test_transient_retry_is_billed_and_recorded(self):
        gen = FakeGen(seedream.ImageError("http", "HTTP 500", http_status=500), None)
        code, run_dir, _ = self.run_costume(costume.Settings("main", "pro", ["char_suwan"], n=1), gen)
        self.assertEqual(code, 0)
        summary, calls = self.load(run_dir)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["status"], "error")
        self.assertEqual(calls[0]["cost_cny"], 0.30)
        self.assertEqual((summary["ok"], summary["first_ok"], summary["transient_failures"]), (1, 0, 1))
        self.assertAlmostEqual(summary["cost_cny_total"], 0.60)

    def test_rejection_not_retried_not_billed(self):
        gen = FakeGen(seedream.ImageError("moderation", "x", http_status=400, api_code="InputTextSensitiveContentDetected"), None)
        code, run_dir, _ = self.run_costume(costume.Settings("main", "pro", ["char_suwan"], n=2), gen)
        self.assertEqual(code, 1)
        summary, calls = self.load(run_dir)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["cost_cny"], 0.0)
        self.assertEqual(summary["failures_by_kind"], {"moderation": 1})
        self.assertEqual(summary["ok"], 1)

    def test_account_level_error_aborts_run(self):
        gen = FakeGen(None, seedream.ImageError("http", "HTTP 403 code=AccountOverdueError", http_status=403, api_code="AccountOverdueError"))
        code, run_dir, _ = self.run_costume(costume.Settings("main", "flash", ["char_suwan"], n=3), gen)
        self.assertEqual(code, 1)
        self.assertEqual(len(gen.calls), 2)
        summary, calls = self.load(run_dir)
        self.assertIn("账号级错误", summary["aborted"])
        self.assertEqual((summary["ok"], summary["n_attempted"]), (1, 2))
        self.assertEqual(summary["failures_by_kind"], {"http": 1})
        self.assertEqual(calls[1]["cost_cny"], 0.0)

    def test_retries_exhausted(self):
        errs = [seedream.ImageError("network", "reset") for _ in range(costume.TRANSIENT_RETRIES + 1)]
        code, run_dir, _ = self.run_costume(costume.Settings("main", "flash", ["char_suwan"], n=1), FakeGen(*errs))
        self.assertEqual(code, 1)
        summary, calls = self.load(run_dir)
        self.assertEqual(len(calls), costume.TRANSIENT_RETRIES + 1)
        self.assertAlmostEqual(summary["cost_cny_total"], 0.12 * 3)
        self.assertEqual(summary["failures_by_kind"], {"network": 1})

    def test_cost_limit_aborts_before_request(self):
        gen = FakeGen()
        code, run_dir, out = self.run_costume(costume.Settings("main", "pro", ["char_suwan"], n=3, max_cost_cny=0.65), gen)
        self.assertEqual(code, 1)
        self.assertEqual(len(gen.calls), 2)
        summary, _ = self.load(run_dir)
        self.assertIn("上限", summary["aborted"])
        self.assertLessEqual(summary["spent_cny_total"], 0.65)

    def test_cost_limit_mid_item_keeps_spent(self):
        gen = FakeGen(seedream.ImageError("http", "HTTP 503", http_status=503))
        code, run_dir, _ = self.run_costume(costume.Settings("main", "pro", ["char_suwan"], n=1, max_cost_cny=0.4), gen)
        self.assertEqual(code, 1)
        summary, calls = self.load(run_dir)
        self.assertEqual(summary["cost_cny_total"], summary["spent_cny_total"])
        self.assertEqual(summary["items"][0]["error_kind"], "aborted")

    def test_ref_mode_sends_base_and_marks_not_eligible(self):
        base = self.base / "base.jpg"
        base.write_bytes(jpeg(1152, 2048, payload=b"base"))
        gen = FakeGen()
        settings = costume.Settings("derive", "pro", ["char_luchen"], mode="ref", bases={"char_luchen": str(base)})
        code, run_dir, _ = self.run_costume(settings, gen)
        self.assertEqual(code, 0)
        self.assertTrue(gen.calls[0]["refs"][0].startswith("data:image/jpeg;base64,"))
        summary, calls = self.load(run_dir)
        self.assertEqual(summary["ok"], 8)
        self.assertTrue(all(not i["seedance_eligible"] and i["refs"] for i in summary["items"]))
        self.assertEqual(summary["bases"]["char_luchen"]["sha256"], images.image_info(base.read_bytes()).sha256)
        self.assertTrue(all(len(json.dumps(c)) < costume.MAX_CALL_LINE_BYTES for c in calls))

    def test_input_errors(self):
        cases = [
            (costume.Settings("main", "pro", ["char_x"]), ENV, "未知角色"),
            (costume.Settings("derive", "pro", ["char_suwan"], mode="ref"), ENV, "主图"),
            (costume.Settings("main", "pro", []), {}, "ARK_API_KEY"),
        ]
        for settings, env, msg in cases:
            with self.subTest(msg=msg):
                out = io.StringIO()
                code = costume.run_costume(settings, env=env, base_dir=self.base / "r", out=out, gen_fn=FakeGen())
                self.assertEqual(code, 2)
                self.assertIn(msg, out.getvalue())


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        out = io.StringIO()
        costume.run_costume(costume.Settings("main", "pro", [], n=2, rounds=2), env=ENV, base_dir=self.base / "runs",
                            out=out, gen_fn=FakeGen(), sleep=lambda s: None)
        self.run_dir = next((self.base / "runs").iterdir())
        self.dest = self.base / "evidence" / "main-pro"
        self.assertEqual(costume.export_run(self.run_dir, self.dest, out=io.StringIO()), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_round1_and_verify_ok(self):
        manifest = json.loads((self.dest / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["images"]), 4)
        self.assertTrue(all(e["round"] == 1 and e["file"].endswith(e["sha256"][:8] + ".jpg") for e in manifest["images"]))
        out = io.StringIO()
        self.assertEqual(costume.run_verify(self.base / "evidence", out=out), 0, out.getvalue())

    def test_export_refuses_nonempty_dest(self):
        self.assertEqual(costume.export_run(self.run_dir, self.dest, out=io.StringIO()), 2)

    def test_verify_detects_tampering(self):
        img = next(self.dest.rglob("*.jpg"))
        img.write_bytes(img.read_bytes() + b"x")
        out = io.StringIO()
        self.assertEqual(costume.run_verify(self.base / "evidence", out=out), 1)
        self.assertIn("sha256", out.getvalue())

    def test_verify_detects_wrong_eligibility_and_missing_file(self):
        m = self.dest / "manifest.json"
        manifest = json.loads(m.read_text(encoding="utf-8"))
        manifest["images"][0]["refs"] = [{"path": "x", "sha256": "0" * 64}]
        (self.dest / manifest["images"][1]["file"]).unlink()
        m.write_text(json.dumps(manifest), encoding="utf-8")
        out = io.StringIO()
        self.assertEqual(costume.run_verify(self.base / "evidence", out=out), 1)
        self.assertIn("seedance_eligible", out.getvalue())
        self.assertIn("缺少", out.getvalue())

    def test_verify_detects_cost_mismatch_and_base64(self):
        calls = self.dest / "run-calls.jsonl"
        lines = calls.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["cost_cny"] = 5.0
        first["extra"]["leak"] = "data:image/jpeg;base64,AAAA"
        calls.write_text("\n".join([json.dumps(first)] + lines[1:]) + "\n", encoding="utf-8")
        out = io.StringIO()
        self.assertEqual(costume.run_verify(self.base / "evidence", out=out), 1)
        self.assertIn("费用合计", out.getvalue())
        self.assertIn("base64", out.getvalue())

    def test_cards(self):
        root = self.base / "evidence"
        self.assertEqual(costume.write_cards(root, out=io.StringIO()), 0)
        card = (root / "char_suwan.md").read_text(encoding="utf-8")
        self.assertIn("main-pro/char_suwan/main-01-", card)
        self.assertIn("✔", card)


class CliTest(unittest.TestCase):
    def parse(self, argv):
        from poc.__main__ import build_parser

        err = io.StringIO()
        import contextlib

        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            args = build_parser().parse_args(["costume", *argv])
            args.func(args)
        return ctx.exception.code, err.getvalue()

    def test_offline_and_generation_are_exclusive(self):
        for argv in (["--verify", "x", "--stage", "main"], ["--export", "a", "b", "--model", "pro"], ["--cards", "x", "--verify", "y"],
                     ["--stage", "main"], ["--stage", "derive", "--model", "pro"], ["--stage", "main", "--model", "pro", "--mode", "ref"], ["--verify", "x", "--small"]):
            with self.subTest(argv=argv):
                code, _ = self.parse(argv)
                self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
