import copy
import io
import json
import re
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from poc import __main__ as cli
from poc import llm, script, shots

SENTINEL = "sk-SENTINEL-7f3a9c2e5b1d4a68"
ENV = {"DEEPSEEK_API_KEY": SENTINEL}
EP01 = json.loads(script.SAMPLE_EP01.read_text(encoding="utf-8"))
USAGE = {"prompt_tokens": 8000, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 8000, "completion_tokens": 6000}


def ok(text):
    return llm.ChatResult(text=text, finish_reason="stop", usage=dict(USAGE), request_id="req", model="deepseek-flash")


def output_of(doc):
    """把 DramaIR 文档的镜头取出来，作为 LLM 的输出格式。"""
    return {"scenes": [{"scene_id": sc["scene_id"], "shots": copy.deepcopy(sc["shots"])} for sc in doc["episodes"][0]["scenes"]]}


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def resplit(doc=EP01):
    """ep01 的另一种合法分镜：台词念得完的镜头前面切出一个 1.5 秒的无台词特写（镜头变多，总时长不变）。"""
    checks = script.drama_ir().checks
    out = output_of(doc)
    for sc in out["scenes"]:
        new = []
        for sh in sc["shots"]:
            speech = sum(checks.speech_seconds(line) for line in sh["dialogue"])
            if sh["duration"]["hint_s"] - 1.5 >= max(speech, 1.5):
                lead = copy.deepcopy(sh)
                lead["dialogue"], lead["duration"]["hint_s"] = [], 1.5
                lead["framing"]["shot_size"] = "CU"
                sh = copy.deepcopy(sh)
                sh["duration"]["hint_s"] -= 1.5
                new.append(lead)
            new.append(sh)
        sc["shots"] = new
    return out


class FakeChat:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, provider, model, messages, **kw):
        self.calls.append({"provider": provider, "model": model, "messages": copy.deepcopy(messages), **kw})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


class ViewAndPromptTest(unittest.TestCase):
    def test_view_has_all_lines_in_order_and_no_shot_structure(self):
        view = shots.script_view(EP01)
        lines = [json.loads(m) for m in re.findall(r"^\[台词\] (.*)$", view, re.M)]
        expected = [line for sc in EP01["episodes"][0]["scenes"] for line in shots.scene_lines(sc)]
        self.assertEqual(lines, expected)
        self.assertEqual(len(lines), 15)
        for sc in EP01["episodes"][0]["scenes"]:
            self.assertIn(f"scene_id={sc['scene_id']}", view)
            for sh in sc["shots"]:
                self.assertNotIn(sh["shot_id"], view)
        for word in ("hint_s", "shot_size", "_sh0"):
            self.assertNotIn(word, view)
        self.assertIn("char_suwan", view)
        self.assertIn("旋转门转动声", view)  # 音效保留在动作描写中

    def test_system_prompt_thresholds_and_schema(self):
        text = shots.system_prompt(EP01)
        self.assertIn("全集共 12 – 24 个镜头", text)
        self.assertIn("在 1.5 – 8 秒之间", text)
        self.assertIn("在 54 – 66 秒之间", text)
        self.assertIn("**全部** 3 场", text)
        self.assertIn(f"{script.drama_ir().checks.CHARS_PER_SECOND:g} × speed", text)
        self.assertLessEqual(set(re.findall(r"\$\w+", text)), {"$defs", "$ref"})

    def test_output_schema_is_extracted_from_v0(self):
        schema = shots.output_schema()
        v0 = script.drama_ir().load_schema("v0")
        self.assertEqual(set(schema["$defs"]), {"shot", "duration", "framing", "shot_character", "line", "delivery"})
        for name, sub in schema["$defs"].items():
            self.assertEqual(sub, v0["$defs"][name])
        self.assertEqual(script.drama_ir().schema.structural_issues(output_of(EP01), schema), [])

    def test_user_prompt_contains_view(self):
        self.assertIn(shots.script_view(EP01), shots.user_prompt(EP01))


class ConservationTest(unittest.TestCase):
    def issues(self, mutate):
        out = output_of(EP01)
        mutate(out)
        return shots.conservation_issues(EP01, out)

    def test_identity_is_conserved(self):
        self.assertEqual(shots.conservation_issues(EP01, output_of(EP01)), [])
        self.assertEqual(shots.conservation_issues(EP01, resplit()), [])

    def test_changed_text(self):
        def m(out):
            out["scenes"][0]["shots"][0]["dialogue"][0]["text"] = "改过的台词"
        self.assertEqual(len(self.issues(m)), 1)
        self.assertTrue(self.issues(m)[0].startswith("$.scenes[0].shots[0].dialogue[0].text: 必须与剧本逐字一致"))

    def test_changed_delivery_and_kind(self):
        def m(out):
            line = out["scenes"][0]["shots"][1]["dialogue"][0]
            line["delivery"]["speed"] = 1.2
            line["kind"] = "dialogue"
        paths = [i.split(":")[0] for i in self.issues(m)]
        self.assertEqual(paths, ["$.scenes[0].shots[1].dialogue[0].kind", "$.scenes[0].shots[1].dialogue[0].delivery"])

    def test_deleted_line(self):
        def m(out):
            out["scenes"][1]["shots"][0]["dialogue"].clear()
        issues = self.issues(m)
        self.assertEqual(len(issues), 1)
        self.assertRegex(issues[0], r"^\$\.scenes\[1\]: 缺少台词 \['l_\d+'")

    def test_moved_across_scenes(self):
        def m(out):
            line = out["scenes"][0]["shots"][0]["dialogue"].pop(0)
            out["scenes"][1]["shots"][0]["dialogue"].append(line)
        issues = self.issues(m)
        self.assertTrue(any("属于场 ep01_sc01，不能挪到本场" in i and i.startswith("$.scenes[1].shots[0]") for i in issues), issues)
        self.assertTrue(any(i.startswith("$.scenes[0]: 缺少台词 ['l_0001']") for i in issues), issues)

    def test_new_and_duplicate_lines(self):
        def m(out):
            d = out["scenes"][0]["shots"][0]["dialogue"]
            d.append(copy.deepcopy(d[0]))
            extra = copy.deepcopy(d[0])
            extra["line_id"] = "l_9999"
            d.append(extra)
        issues = self.issues(m)
        self.assertTrue(any("l_0001 重复出现" in i for i in issues), issues)
        self.assertTrue(any("l_9999 剧本中没有这句台词" in i for i in issues), issues)

    def test_reordered_lines(self):
        def m(out):
            a = out["scenes"][0]["shots"][0]["dialogue"]
            b = out["scenes"][0]["shots"][1]["dialogue"]
            a[:], b[:] = b[:], a[:]
        issues = self.issues(m)
        self.assertEqual(len(issues), 1)
        self.assertIn("$.scenes[0]: 台词顺序与剧本不一致", issues[0])

    def test_scene_ids_must_match(self):
        def m(out):
            out["scenes"].pop()
        self.assertTrue(self.issues(m)[0].startswith("$.scenes: 场必须与剧本完全一致"))

    def test_empty_scene(self):
        def m(out):
            out["scenes"][2]["shots"] = []
        self.assertIn("$.scenes[2].shots: 每场至少需要 1 个镜头", self.issues(m))


class MergeAndGateTest(unittest.TestCase):
    def test_merge_renumbers_and_keeps_rest(self):
        out = resplit()
        out["scenes"][0]["shots"][0]["shot_id"] = "whatever"
        doc = shots.merge(EP01, out)
        ids = [sh["shot_id"] for sc in doc["episodes"][0]["scenes"] for sh in sc["shots"]]
        self.assertEqual(ids[0], "ep01_sc01_sh01")
        self.assertEqual(len(ids), len(set(ids)))
        for sc in doc["episodes"][0]["scenes"]:
            self.assertEqual([sh["shot_id"] for sh in sc["shots"]], [f"{sc['scene_id']}_sh{k:02d}" for k in range(1, len(sc["shots"]) + 1)])
        self.assertEqual(doc["characters"], EP01["characters"])
        self.assertEqual(doc["series"], EP01["series"])
        self.assertTrue(script.drama_ir().validate(doc).ok(strict=True))
        self.assertEqual(out["scenes"][0]["shots"][0]["shot_id"], "whatever")  # 不改输入

    def test_ep01_passes_all_gates(self):
        self.assertEqual(shots.gate_issues(EP01), [])

    def test_ranges(self):
        self.assertEqual(shots.shot_count_range(60), (12, 24))
        self.assertEqual(shots.shot_count_range(90), (18, 36))
        self.assertEqual(shots.total_range(60), (54.0, 66.0))

    def gates(self, mutate):
        doc = copy.deepcopy(EP01)
        mutate(doc)
        return [g for g, _ in shots.gate_issues(doc)]

    def test_h1_boundaries(self):
        def with_n(k):  # 前两场 9 个镜头不动，第 3 场凑成 k - 9 个；只看 H1
            def m(doc):
                scene = doc["episodes"][0]["scenes"][2]
                scene["shots"] = [copy.deepcopy(scene["shots"][-1]) for _ in range(k - 9)]
            return m
        self.assertNotIn("H1", self.gates(with_n(12)))
        self.assertIn("H1", self.gates(with_n(11)))
        self.assertNotIn("H1", self.gates(with_n(24)))
        self.assertIn("H1", self.gates(with_n(25)))

    def test_h2_boundaries(self):
        def with_hint(v):
            def m(doc):
                doc["episodes"][0]["scenes"][0]["shots"][0]["duration"]["hint_s"] = v
            return m
        for v, bad in ((1.5, False), (1.4, True), (8, False), (8.1, True)):
            self.assertEqual("H2" in self.gates(with_hint(v)), bad, v)

    def test_h3_boundaries(self):
        def shift(delta):  # 基线合计 60 秒，允许 54–66 秒；只看 H3
            def m(doc):
                doc["episodes"][0]["scenes"][0]["shots"][0]["duration"]["hint_s"] += delta
            return m
        for delta, bad in ((6, False), (-6, False), (6.5, True), (-6.5, True)):
            self.assertEqual("H3" in self.gates(shift(delta)), bad, delta)

    def test_h4(self):
        def m(doc):
            doc["episodes"][0]["scenes"][0]["shots"][0]["duration"]["hint_s"] = 2  # l_0001 约 3.7 秒
        issues = [msg for g, msg in shots.gate_issues(self._mut(m)) if g == "H4"]
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].startswith("$.episodes[0].scenes[0].shots[0].duration.hint_s"))

    def test_h4_equal_is_ok(self):
        checks = script.drama_ir().checks
        doc = copy.deepcopy(EP01)
        shot = doc["episodes"][0]["scenes"][0]["shots"][0]
        shot["duration"]["hint_s"] = sum(checks.speech_seconds(line) for line in shot["dialogue"])  # 正好念完
        self.assertNotIn("H4", [g for g, _ in shots.gate_issues(doc)])

    def _mut(self, mutate):
        doc = copy.deepcopy(EP01)
        mutate(doc)
        return doc


class MetricsTest(unittest.TestCase):
    def test_ep01_anchor(self):
        m = shots.metrics(EP01)
        self.assertEqual((m["shots"], m["total_s"], m["shot_s_avg"]), (13, 60, 4.615))
        self.assertEqual((m["shot_s_min"], m["shot_s_max"], m["shots_over_5s"]), (4, 5, 0))
        self.assertEqual(m["shot_size_kinds"], 6)
        self.assertEqual(m["close_up_ratio"], round(7 / 13, 3))
        self.assertEqual((m["lines"], m["max_lines_per_shot"], m["multi_speaker_shots"]), (15, 2, 1))
        self.assertEqual(m["establishing_first"], 3)
        self.assertEqual(m["gates"], {"H1": True, "H2": True, "H3": True, "H4": True})
        self.assertNotIn("vs_source", m)

    def test_vs_source(self):
        same = shots.metrics(EP01, EP01)["vs_source"]
        self.assertEqual(same, {"source_shots": 13, "shot_count_delta": 0, "same_line_groups_ratio": 1.0, "identical_line_grouping": True})
        doc = shots.merge(EP01, resplit())
        other = shots.metrics(doc, EP01)["vs_source"]
        self.assertGreater(other["shot_count_delta"], 0)
        self.assertTrue(other["identical_line_grouping"])  # 只拆了无台词镜头

    def test_metrics_cli(self):
        out = io.StringIO()
        code = shots.run_metrics([script.SAMPLE_EP01], out=out)
        self.assertEqual(code, 0)
        self.assertIn("H1✓ H2✓ H3✓ H4✓", out.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            bad = copy.deepcopy(EP01)
            bad["episodes"][0]["scenes"][0]["shots"][0]["duration"]["hint_s"] = 9
            path = Path(tmp) / "bad.json"
            path.write_text(dumps(bad), encoding="utf-8")
            out = io.StringIO()
            self.assertEqual(shots.run_metrics([path], out=out), 1)
            self.assertIn("H2✗", out.getvalue())
            out = io.StringIO()
            self.assertEqual(shots.run_metrics([path], source=script.SAMPLE_EP01, as_json=True, out=out), 1)
            self.assertIn("vs_source", json.loads(out.getvalue())[0])

    def test_no_dialogue_and_non_object_inputs(self):
        doc = copy.deepcopy(EP01)
        for sh in shots.shots_of(doc):
            sh["dialogue"] = []
        self.assertTrue(script.drama_ir().validate(doc).ok(strict=True))
        self.assertIn("留白≥0.3s -", shots.metrics_line(shots.metrics(doc)))
        with tempfile.TemporaryDirectory() as tmp:
            nodlg, arr = Path(tmp) / "nodlg.json", Path(tmp) / "arr.json"
            nodlg.write_text(dumps(doc), encoding="utf-8")
            arr.write_text("[1, 2]", encoding="utf-8")
            out = io.StringIO()
            self.assertEqual(shots.run_metrics([nodlg], out=out), 0)
            out = io.StringIO()
            self.assertEqual(shots.run_metrics([arr], out=out), 1)
            self.assertIn("无法计算指标", out.getvalue())

    def test_cli_rejects_mixed_modes(self):
        for argv in (["shots", "--source", "x.json"], ["shots", "--json"], ["shots", "--metrics", "a.json", "--n", "2"],
                     ["shots", "--metrics", "a.json", "--input", "b.json"]):
            args = cli.build_parser().parse_args(argv)
            with self.assertRaises(SystemExit) as cm, unittest.mock.patch("sys.stderr", io.StringIO()):
                args.func(args)
            self.assertEqual(cm.exception.code, 2, argv)

    def test_cli_parser(self):
        args = cli.build_parser().parse_args(["shots", "--n", "2", "--thinking", "disabled"])
        self.assertEqual((args.n, args.thinking, args.prompt_version, args.model), (2, "disabled", "shots.v1", "deepseek-flash"))
        self.assertIsNone(cli.build_parser().parse_args(["shots"]).n)  # 未给出时按 5 份
        self.assertEqual(cli.build_parser().parse_args(["script"]).prompt_version, "script.v1")


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def run_shots(self, chat, n=1, inputs=None, **settings):
        out = io.StringIO()
        code = shots.run_shots(
            shots.ShotSettings(**settings), inputs=inputs, n=n, env=ENV, chat_fn=chat,
            balance_fn=lambda p: None, base_dir=self.base, out=out, sleep=lambda s: None,
        )
        run_dir = next(self.base.iterdir())
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        return code, run_dir, summary, out.getvalue()

    def test_first_pass(self):
        chat = FakeChat(ok(dumps(resplit())))
        code, run_dir, summary, out = self.run_shots(chat)
        self.assertEqual(code, 0, out)
        self.assertEqual((summary["passed"], summary["first_pass"]), (1, 1))
        s01 = run_dir / "samples" / "s01"
        final = json.loads((s01 / "final.json").read_text(encoding="utf-8"))
        self.assertEqual(final, shots.merge(EP01, resplit()))
        m = json.loads((s01 / "metrics.json").read_text(encoding="utf-8"))
        self.assertTrue(all(m["gates"].values()))
        self.assertIn("vs_source", m)
        self.assertEqual(summary["samples"][0]["input"], "packages/drama-ir/examples/v0/ep01.json")
        self.assertEqual(summary["inputs"][0]["sha256"], "cd02daef022c6e78227ef3bf366adb2b5142a979a803bcf4373cdb8765a6fb59")
        call = chat.calls[0]
        self.assertTrue(call["json_mode"])
        self.assertIn("分镜师", call["messages"][0]["content"])
        self.assertIn("[台词]", call["messages"][1]["content"])
        self.assertNotIn(SENTINEL, "".join(p.read_text(encoding="utf-8") for p in run_dir.rglob("*") if p.is_file()))

    def test_conservation_failure_then_repair(self):
        bad = output_of(EP01)
        bad["scenes"][0]["shots"][0]["dialogue"][0]["text"] = "改过的台词"
        chat = FakeChat(ok(dumps(bad)), ok(dumps(output_of(EP01))))
        code, run_dir, summary, _ = self.run_shots(chat)
        self.assertEqual(code, 0)
        self.assertEqual(summary["repairs"], [1])
        repair = chat.calls[1]["messages"][3]["content"]
        self.assertIn("$.scenes[0].shots[0].dialogue[0].text", repair)

    def test_gate_failure_is_repaired_with_rewritten_paths(self):
        bad = output_of(EP01)
        bad["scenes"][0]["shots"][0]["duration"]["hint_s"] = 9  # H2 错误 + H3 偏差 5 秒以内
        bad["scenes"][0]["shots"][1]["duration"]["hint_s"] = 1  # H2 错误，同时台词念不完（校验器警告，H4 不重复）
        chat = FakeChat(ok(dumps(bad)), ok(dumps(output_of(EP01))))
        code, run_dir, summary, _ = self.run_shots(chat)
        self.assertEqual(code, 0)
        report = json.loads((run_dir / "samples/s01/attempt0.report.json").read_text(encoding="utf-8"))
        issues = report["issues"]
        self.assertTrue(any(i.startswith("错误 $.scenes[0].shots[0].duration.hint_s: 单镜时长 9") for i in issues), issues)
        self.assertTrue(any(i.startswith("警告 $.scenes[0].shots[1].duration.hint_s: 台词估算朗读") for i in issues), issues)
        self.assertFalse(any(i.startswith("错误") and "超过镜头时长" in i for i in issues), issues)  # H4 不重复回喂

    def test_structural_error_is_reported_on_output_paths(self):
        bad = output_of(EP01)
        bad["scenes"][0]["shots"][0]["framing"]["shot_size"] = "WIDE"
        chat = FakeChat(ok(dumps(bad)))
        code, run_dir, summary, _ = self.run_shots(chat, max_repairs=0)
        self.assertEqual(code, 1)
        report = json.loads((run_dir / "samples/s01/attempt0.report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["issues"][0].startswith("错误 $.scenes[0].shots[0].framing.shot_size"))
        self.assertFalse((run_dir / "samples/s01/metrics.json").exists())

    def test_cost_limit_aborts_and_counts(self):
        chat = FakeChat(ok(dumps(output_of(EP01))))
        code, run_dir, summary, out = self.run_shots(chat, n=5, max_cost_cny=0.1)
        self.assertEqual(code, 1)
        self.assertIsNotNone(summary["aborted"])
        self.assertLess(summary["passed"], 5)
        calls = (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertAlmostEqual(summary["spent_cny_total"], round(sum(json.loads(c)["cost_cny"] for c in calls), 4))

    def test_multiple_inputs_share_cost_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            p2 = Path(tmp) / "ep01_copy.json"
            p2.write_text(dumps(EP01), encoding="utf-8")
            chat = FakeChat(ok(dumps(output_of(EP01))))
            code, run_dir, summary, _ = self.run_shots(chat, n=1, inputs=[script.SAMPLE_EP01, p2])
        self.assertEqual(code, 0)
        self.assertEqual([s["sample"] for s in summary["samples"]], ["in01_s01", "in02_s01"])
        self.assertEqual((summary["n_requested"], summary["n_expected"]), (1, 2))
        self.assertEqual(len(summary["inputs"]), 2)
        # 一次调用 ¥0.064，上限 ¥0.05：第一份输入的请求照常发出，第二份输入的首个请求前按整次运行的累计费用中止
        out = io.StringIO()
        code = shots.run_shots(
            shots.ShotSettings(max_cost_cny=0.05), inputs=[script.SAMPLE_EP01, script.SAMPLE_EP01], n=1, env=ENV,
            chat_fn=FakeChat(ok(dumps(output_of(EP01)))), balance_fn=lambda p: None, base_dir=self.base / "b", out=out,
        )
        self.assertEqual(code, 1)
        self.assertIn("in02_s01: 中止", out.getvalue())

    def test_invalid_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = copy.deepcopy(EP01)
            bad["episodes"][0]["target_duration_s"] = 200  # 严格校验的警告
            path = Path(tmp) / "bad.json"
            path.write_text(dumps(bad), encoding="utf-8")
            out = io.StringIO()
            code = shots.run_shots(shots.ShotSettings(), inputs=[path], env=ENV, chat_fn=FakeChat(ok("{}")), base_dir=self.base, out=out)
        self.assertEqual(code, 2)
        self.assertIn("严格校验", out.getvalue())
        self.assertEqual(list(self.base.iterdir()), [])

    def test_missing_key(self):
        out = io.StringIO()
        self.assertEqual(shots.run_shots(shots.ShotSettings(), env={}, out=out, base_dir=self.base), 2)
        self.assertIn("DEEPSEEK_API_KEY", out.getvalue())


if __name__ == "__main__":
    unittest.main()
