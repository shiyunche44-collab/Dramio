import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from poc import __main__ as cli
from poc import llm, script

SENTINEL = "sk-SENTINEL-7f3a9c2e5b1d4a68"
ENV = {"DEEPSEEK_API_KEY": SENTINEL}
EP01_TEXT = script.SAMPLE_EP01.read_text(encoding="utf-8")
EP01 = json.loads(EP01_TEXT)
LOGLINE = EP01["series"]["logline"]
USAGE = {"prompt_tokens": 8000, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 8000, "completion_tokens": 6000}


def ok(text):
    return llm.ChatResult(text=text, finish_reason="stop", usage=dict(USAGE), request_id="req", model="deepseek-flash")


def broken_ep01():
    doc = copy.deepcopy(EP01)
    doc["episodes"][0]["scenes"][0]["shots"][0]["dialogue"][0]["speaker"] = "char_nobody"
    return json.dumps(doc, ensure_ascii=False)


class FakeChat:
    """按顺序返回预设结果；元素为 ChatResult 或要抛出的异常。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, provider, model, messages, **kw):
        self.calls.append({"provider": provider, "model": model, "messages": copy.deepcopy(messages), **kw})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


class ScriptTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def run_script(self, chat, n=1, **settings):
        out = io.StringIO()
        code = script.run_script(
            script.Settings(**settings), n=n, env=ENV, chat_fn=chat,
            balance_fn=lambda p: None, base_dir=self.base, out=out, sleep=lambda s: None,
        )
        run_dir = next(self.base.iterdir())
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        return code, run_dir, summary, out.getvalue()


class PromptTest(unittest.TestCase):
    def test_system_prompt_has_schema_and_thresholds(self):
        checks = script.drama_ir().checks
        text = script.system_prompt(60)
        self.assertIn('"$defs"', text)
        self.assertNotIn('"$id"', text)
        self.assertIn(checks.ID_PATTERN.pattern, text)
        self.assertIn(f"hint_s ≤ {checks.MAX_SHOT_S:g}", text)
        self.assertIn(f"{checks.CHARS_PER_SECOND:g} × speed", text)
        self.assertIn("48 – 72 秒", text)  # 60 × (1 ± 20%)
        self.assertLessEqual(set(re.findall(r"\$\w+", text)), {"$defs", "$ref"})  # 没有残留的模板占位符

    def test_llm_schema_does_not_mutate_cached_schema(self):
        script.llm_schema()
        self.assertIn("$id", script.drama_ir().load_schema("v0"))

    def test_default_logline_is_ep01(self):
        logline, sha = script.default_logline()
        self.assertEqual(logline, LOGLINE)
        self.assertEqual(sha, "cd02daef022c6e78227ef3bf366adb2b5142a979a803bcf4373cdb8765a6fb59")

    def test_repair_prompt_truncates(self):
        text = script.repair_prompt([f"错误 $.x[{i}]: bad" for i in range(40)])
        self.assertIn("共 40 个问题", text)
        self.assertIn("$.x[29]", text)
        self.assertNotIn("$.x[30]", text)
        self.assertIn("另有 10 条", text)


class GenerateTest(ScriptTestCase):
    def test_first_pass(self):
        chat = FakeChat(ok(EP01_TEXT))
        code, run_dir, summary, out = self.run_script(chat)
        self.assertEqual(code, 0)
        self.assertEqual((summary["passed"], summary["first_pass"], summary["repairs"]), (1, 1, [0]))
        s01 = run_dir / "samples" / "s01"
        self.assertEqual(json.loads((s01 / "final.json").read_text(encoding="utf-8")), EP01)
        self.assertTrue((s01 / "final.md").read_text(encoding="utf-8").startswith("#"))
        self.assertEqual(summary["samples"][0]["stats"]["shots"], 13)
        self.assertEqual(summary["logline_source"]["type"], "sample")
        for name in ("meta.json", "calls.jsonl", "prompt.json", "summary.json"):
            self.assertTrue((run_dir / name).is_file(), name)
        # 请求参数：JSON 模式、模型、user 消息含梗概
        call = chat.calls[0]
        self.assertTrue(call["json_mode"])
        self.assertEqual(call["model"], "deepseek-flash")
        self.assertIn(LOGLINE, call["messages"][1]["content"])

    def test_repair_then_pass(self):
        chat = FakeChat(ok(broken_ep01()), ok(EP01_TEXT))
        code, run_dir, summary, _ = self.run_script(chat)
        self.assertEqual(code, 0)
        self.assertEqual((summary["passed"], summary["first_pass"], summary["repairs"]), (1, 0, [1]))
        repair_msgs = chat.calls[1]["messages"]
        self.assertEqual(repair_msgs[2]["role"], "assistant")
        self.assertIn("$.episodes[0].scenes[0].shots[0].dialogue[0].speaker", repair_msgs[3]["content"])
        report0 = json.loads((run_dir / "samples/s01/attempt0.report.json").read_text(encoding="utf-8"))
        self.assertEqual(report0["outcome"], "invalid")

    def test_always_invalid_fails_after_max_repairs(self):
        chat = FakeChat(ok(broken_ep01()))
        code, run_dir, summary, _ = self.run_script(chat, max_repairs=2)
        self.assertEqual(code, 1)
        self.assertEqual(len(chat.calls), 3)
        self.assertEqual(summary["passed"], 0)
        self.assertEqual(summary["repairs"], [2])
        self.assertFalse((run_dir / "samples/s01/final.json").exists())
        self.assertEqual(summary["failed_attempts_by_kind"], {"invalid": 3})

    def test_not_json_is_repaired(self):
        chat = FakeChat(ok("好的，这是剧本：{"), ok(EP01_TEXT))
        code, _, summary, _ = self.run_script(chat)
        self.assertEqual(code, 0)
        self.assertEqual([a["outcome"] for a in summary["samples"][0]["attempts"]], ["not_json", "pass"])
        self.assertIn("不是合法 JSON", chat.calls[1]["messages"][3]["content"])

    def test_nan_is_not_json(self):
        chat = FakeChat(ok(EP01_TEXT.replace('"hint_s": 4', '"hint_s": NaN', 1)), ok(EP01_TEXT))
        _, _, summary, _ = self.run_script(chat)
        self.assertEqual(summary["samples"][0]["attempts"][0]["outcome"], "not_json")

    def test_warnings_fail_strict(self):
        doc = copy.deepcopy(EP01)
        doc["episodes"][0]["target_duration_s"] = 200  # 时长偏差 → 警告
        chat = FakeChat(ok(json.dumps(doc, ensure_ascii=False)))
        code, _, summary, _ = self.run_script(chat, max_repairs=0)
        self.assertEqual(code, 1)
        a = summary["samples"][0]["attempts"][0]
        self.assertEqual((a["outcome"], a["errors"], a["warnings"]), ("invalid", 0, 1))

    def test_logline_must_match_input(self):
        chat = FakeChat(ok(EP01_TEXT))
        out = io.StringIO()
        code = script.run_script(
            script.Settings(max_repairs=0), n=1, logline="另一个梗概", env=ENV, chat_fn=chat,
            balance_fn=lambda p: None, base_dir=self.base, out=out,
        )
        self.assertEqual(code, 1)
        report = json.loads(next(self.base.glob("*/samples/s01/attempt0.report.json")).read_text(encoding="utf-8"))
        self.assertTrue(any("logline" in i for i in report["issues"]))

    def test_single_episode_required(self):
        doc = copy.deepcopy(EP01)
        ep2 = copy.deepcopy(doc["episodes"][0])
        text = json.dumps(ep2, ensure_ascii=False)
        text = text.replace('"ep01', '"ep02').replace('"l_00', '"l_20')
        ep2 = json.loads(text)
        ep2["number"] = 2
        doc["episodes"].append(ep2)
        chat = FakeChat(ok(json.dumps(doc, ensure_ascii=False)))
        _, run_dir, summary, _ = self.run_script(chat, max_repairs=0)
        self.assertEqual(summary["passed"], 0)
        report = json.loads((run_dir / "samples/s01/attempt0.report.json").read_text(encoding="utf-8"))
        self.assertIn("错误 $.episodes: 应只有 1 集，实际 2 集", report["issues"])

    def test_truncated_regenerates_from_scratch(self):
        trunc = llm.LLMError("truncated", "输出被截断", usage=dict(USAGE))
        chat = FakeChat(trunc, ok(EP01_TEXT))
        code, run_dir, summary, _ = self.run_script(chat)
        self.assertEqual(code, 0)
        self.assertEqual(len(chat.calls[1]["messages"]), 2)  # 没有可修复的输出，从头生成
        calls = [json.loads(line) for line in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls[0]["status"], "error")
        self.assertGreater(calls[0]["cost_cny"], 0)  # 截断也计费
        self.assertEqual(calls[0]["extra"]["error_kind"], "truncated")

    def test_transient_errors_retry_without_using_repairs(self):
        chat = FakeChat(llm.LLMError("network", "x"), llm.LLMError("http", "busy", http_status=503), ok(EP01_TEXT))
        code, run_dir, summary, _ = self.run_script(chat, max_repairs=0)
        self.assertEqual(code, 0)
        self.assertEqual(summary["samples"][0]["attempts"][0]["requests"], 3)
        self.assertEqual(len((run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()), 3)

    def test_auth_error_is_not_retried(self):
        chat = FakeChat(llm.LLMError("http", "HTTP 401", http_status=401))
        code, _, summary, _ = self.run_script(chat, max_repairs=0)
        self.assertEqual(code, 1)
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(summary["failed_attempts_by_kind"], {"http": 1})

    def test_cost_limit_aborts(self):
        chat = FakeChat(ok(broken_ep01()))
        code, _, summary, out = self.run_script(chat, n=5, max_cost_cny=0.01)
        self.assertEqual(code, 1)
        self.assertEqual(len(chat.calls), 1)  # 第 1 次后已超限
        self.assertIn("已达上限", summary["aborted"])
        self.assertIn("中止", out)

    def test_calls_jsonl_and_no_secret_leak(self):
        chat = FakeChat(ok(broken_ep01()), ok(EP01_TEXT))
        self.run_script(chat, n=2)
        run_dir = next(self.base.iterdir())
        calls = [json.loads(line) for line in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 3)  # s01: 2 次；s02: 1 次
        for c in calls:
            self.assertEqual((c["provider"], c["capability"], c["model"], c["cost_basis"]), ("deepseek", "llm", "deepseek-flash", "estimate"))
            self.assertIsNotNone(c["usage"])
            self.assertIn("attempt", c["extra"])
            self.assertEqual(c["extra"]["prompt_version"], "script.v1")
        self.assertEqual([c["extra"]["attempt"] for c in calls], [0, 1, 0])
        for path in run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(SENTINEL, path.read_text(encoding="utf-8"), path)

    def test_thinking_and_effort_passed_through(self):
        chat = FakeChat(ok(EP01_TEXT))
        self.run_script(chat, thinking="disabled", reasoning_effort="low", temperature=0.8)
        call = chat.calls[0]
        self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}, "reasoning_effort": "low"})
        self.assertEqual(call["temperature"], 0.8)


class CliTest(unittest.TestCase):
    def test_parser_defaults(self):
        args = cli.build_parser().parse_args(["script"])
        self.assertEqual((args.n, args.model, args.max_repairs, args.logline), (5, "deepseek-flash", 2, None))

    def test_missing_key_returns_2(self):
        out = io.StringIO()
        code = script.run_script(script.Settings(), env={}, chat_fn=FakeChat(ok(EP01_TEXT)), out=out)
        self.assertEqual(code, 2)
        self.assertIn("DEEPSEEK_API_KEY", out.getvalue())

    def test_unknown_provider_returns_2(self):
        out = io.StringIO()
        self.assertEqual(script.run_script(script.Settings(provider="nope"), env=ENV, out=out), 2)


if __name__ == "__main__":
    unittest.main()
