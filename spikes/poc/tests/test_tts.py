import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from poc import __main__ as cli
from poc import speech, tts
from tests.test_audio import frame_v2, id3

ENV = {"VOLC_SPEECH_API_KEY": "vs-SENTINEL-9d2f"}
VOICES = {"char_suwan": "zh_female_a_uranus_bigtts", "char_luchen": "zh_male_b_uranus_bigtts"}


def fake_mp3(text):
    """每个字 5 帧（0.12 秒）的假音频。"""
    return id3() + frame_v2() * (5 * len(text))


class FakeTTS:
    def __init__(self, failures=None):
        self.failures = dict(failures or {})  # text → 依次抛出的异常列表
        self.calls = []

    def __call__(self, text, voice, *, resource, speech_rate, context_text, env):
        self.calls.append({"text": text, "voice": voice, "speech_rate": speech_rate, "context_text": context_text})
        pending = self.failures.get(text)
        if pending:
            raise pending.pop(0)
        return speech.TTSResult(fake_mp3(text), len(text), 3, "log")


class FakeASR:
    """submit 记录音频；query 先返回一次“处理中”，再返回按原文（或覆盖文本）转写的结果。"""

    def __init__(self, override=None):
        self.override = override or {}
        self.jobs = {}
        self.pending = set()

    def submit(self, mp3, *, env):
        rid = f"rid-{len(self.jobs)}"
        self.jobs[rid] = mp3
        self.pending.add(rid)
        return rid

    def query(self, rid, *, env):
        if rid in self.pending:
            self.pending.discard(rid)
            return 20000001, None
        seconds = tts.audio.mp3_info(self.jobs[rid]).duration_s
        text = self.override.get(rid, self.current_text)
        return 20000000, {
            "audio_info": {"duration": round(seconds * 1000)},
            "result": {"text": text, "utterances": [{"text": text, "start_time": 100, "end_time": round(seconds * 1000) - 100}]},
        }


def line(line_id="l_0001", text="你好。", emotion="angry", intensity=0.8, speed=1.1, kind="dialogue", speaker="char_suwan"):
    return {
        "line_id": line_id, "speaker": speaker, "kind": kind, "text": text, "shot_id": "ep01_sc01_sh01",
        "delivery": {"emotion": emotion, "intensity": intensity, "speed": speed},
    }


class MappingTest(unittest.TestCase):
    def test_instruction(self):
        self.assertEqual(tts.instruction(line()), "你可以用非常愤怒的语气说这句话吗？")
        self.assertEqual(tts.instruction(line(emotion="sad", intensity=0.4, kind="voiceover")), "这是内心独白，你可以压低声音、用略带难过低落的语气说这句话吗？")
        self.assertEqual(tts.instruction(line(emotion="happy", intensity=0.5)), "你可以用开心得意的语气说这句话吗？")

    def test_every_schema_emotion_is_mapped(self):
        schema = json.loads((tts.script.REPO_ROOT / "packages/drama-ir/schema/v0/drama.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["$defs"]["delivery"]["properties"]["emotion"]["enum"]), set(tts.EMOTION_ZH))

    def test_speech_rate(self):
        self.assertEqual([tts.speech_rate(s) for s in (0.9, 1.0, 1.1, 0.3, 2.5)], [-10, 0, 10, -50, 100])


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def run_tts(self, settings, tts_fn=None, asr=None):
        asr = asr or FakeASR()
        tts_fn = tts_fn or FakeTTS()
        orig_submit = asr.submit

        def submit(mp3, *, env):
            asr.current_text = self.current.get(len(asr.jobs), "")
            return orig_submit(mp3, env=env)

        doc, _ = tts.load_episode()
        texts = [ln["text"] for ln in tts.episode_lines(doc) if settings.only is None or ln["speaker"] == settings.only]
        self.current = {i: t for i, t in enumerate(texts * settings.rounds)}
        out = io.StringIO()
        code = tts.run_tts(settings, env=ENV, base_dir=self.base, out=out, tts_fn=tts_fn, submit_fn=submit, query_fn=asr.query, sleep=lambda s: None)
        run_dir = next(self.base.iterdir())
        return code, run_dir, json.loads((run_dir / "summary.json").read_text(encoding="utf-8")), out.getvalue()

    def calls(self, run_dir):
        return [json.loads(x) for x in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_full_episode_round_trip(self):
        code, run_dir, s, _ = self.run_tts(tts.Settings("A", VOICES, instruct=True, speed=True, rounds=2))
        self.assertEqual(code, 0)
        self.assertEqual((s["n_expected"], s["ok"], s["first_ok"], s["transient_failures"]), (30, 30, 30, 0))
        self.assertEqual(s["cer_avg"], 0.0)
        self.assertEqual(len(list((run_dir / "samples" / "r1").glob("l_*.mp3"))), 15)
        self.assertEqual(len(list((run_dir / "samples" / "r2" / "asr").glob("l_*.json"))), 15)
        calls = self.calls(run_dir)
        self.assertTrue(all(c["provider"] == "volc_speech" and c["capability"] in ("tts", "asr") for c in calls))
        self.assertTrue(all(c["cost_basis"] in ("estimate", "free") for c in calls))
        self.assertAlmostEqual(sum(c["cost_cny"] for c in calls), s["spent_cny_total"], places=6)
        self.assertAlmostEqual(s["cost_cny_total"], s["spent_cny_total"], places=6)
        # 每句 1 次 TTS + 1 次 submit + 2 次 query
        self.assertEqual(len(calls), 30 * 4)
        first = s["lines"][0]
        self.assertEqual(first["context_text"], tts.instruction({**tts.episode_lines(tts.load_episode()[0])[0]}))
        self.assertEqual(first["speech_rate"], -10)  # l_0001 speed 0.9
        self.assertEqual(len(s["rounds"]), 2)
        self.assertEqual(s["rounds"][0]["stats"]["shots_checked"], 13)  # 15 句分布在 13 个镜头（sc01_sh05、sc03_sh03 各两句）
        self.assertNotIn(ENV["VOLC_SPEECH_API_KEY"], (run_dir / "calls.jsonl").read_text(encoding="utf-8"))

    def test_transient_failure_is_retried_and_accounted(self):
        text = "我以为，努力就够了。"
        fake = FakeTTS({text: [speech.SpeechError("truncated", "没有结束块")]})
        code, run_dir, s, _ = self.run_tts(tts.Settings("A", VOICES, rounds=1), tts_fn=fake)
        self.assertEqual(code, 0)
        self.assertEqual((s["ok"], s["first_ok"], s["transient_failures"], s["tts_requests"]), (15, 14, 1, 16))
        tts_calls = [c for c in self.calls(run_dir) if c["capability"] == "tts" and c["extra"]["line_id"] == "l_0002"]
        self.assertEqual([c["status"] for c in tts_calls], ["error", "ok"])
        self.assertGreater(tts_calls[0]["cost_cny"], 0)  # 截断的请求保守计费
        self.assertEqual(tts_calls[0]["extra"]["error_kind"], "truncated")

    def test_rejected_request_is_not_retried_or_charged(self):
        text = "我以为，努力就够了。"
        fake = FakeTTS({text: [speech.SpeechError("api_error", "code=45000000 speaker permission denied", api_code=45000000)]})
        code, run_dir, s, _ = self.run_tts(tts.Settings("A", VOICES, rounds=1), tts_fn=fake)
        self.assertEqual(code, 1)
        self.assertEqual((s["ok"], s["tts_requests"]), (14, 15))
        bad = [x for x in s["lines"] if x["line_id"] == "l_0002"][0]
        self.assertEqual((bad["ok"], bad["stage"]), (False, "tts"))
        call = [c for c in self.calls(run_dir) if c["capability"] == "tts" and c["extra"]["line_id"] == "l_0002"]
        self.assertEqual((len(call), call[0]["cost_cny"]), (1, 0.0))

    def test_retries_exhausted(self):
        text = "我以为，努力就够了。"
        fake = FakeTTS({text: [speech.SpeechError("network", "reset")] * 3})
        code, _, s, _ = self.run_tts(tts.Settings("A", VOICES, rounds=1), tts_fn=fake)
        self.assertEqual(code, 1)
        bad = [x for x in s["lines"] if x["line_id"] == "l_0002"][0]
        self.assertEqual((bad["tts_requests"], bad["transient_failures"]), (3, 2))

    def test_query_failure_is_retried_without_resubmit(self):
        asr = FakeASR()
        orig = asr.query
        failed = []

        def query(rid, *, env):
            if rid == "rid-0" and not failed:
                failed.append(rid)
                raise speech.SpeechError("network", "reset")
            return orig(rid, env=env)

        asr.query = query
        code, run_dir, s, _ = self.run_tts(tts.Settings("A", VOICES, rounds=1), asr=asr)
        self.assertEqual(code, 0)
        self.assertEqual((s["asr_requests"], s["transient_failures"], s["first_ok"]), (15, 1, 15))
        first = [c for c in self.calls(run_dir) if c["capability"] == "asr" and c["extra"]["line_id"] == "l_0001"]
        self.assertEqual([c["extra"]["op"] for c in first], ["submit", "query", "query", "query"])
        self.assertEqual({c["request_id"] for c in first}, {"rid-0"})
        self.assertEqual([c["status"] for c in first], ["ok", "error", "ok", "ok"])

    def test_submit_network_failure_is_charged(self):
        asr = FakeASR()
        orig = asr.submit
        failed = []

        def submit(mp3, *, env):
            if not failed:
                failed.append(1)
                raise speech.SpeechError("network", "reset")
            return orig(mp3, env=env)

        asr.submit = submit
        code, run_dir, s, _ = self.run_tts(tts.Settings("A", VOICES, rounds=1), asr=asr)
        self.assertEqual(code, 0)
        bad = [c for c in self.calls(run_dir) if c["extra"].get("op") == "submit" and c["status"] == "error"]
        self.assertEqual(len(bad), 1)
        self.assertGreater(bad[0]["cost_cny"], 0)  # 响应丢失时供应商可能已受理，保守计费
        self.assertAlmostEqual(s["cost_cny_total"], s["spent_cny_total"], places=6)

    def test_cost_limit_mid_line_keeps_its_cost(self):
        # l_0001 的 TTS 约 ¥0.0048；上限设在 TTS 之后、ASR 之前触发
        code, run_dir, s, _ = self.run_tts(tts.Settings("A", VOICES, rounds=1, max_cost_cny=0.004))
        self.assertEqual(code, 1)
        self.assertIsNotNone(s["aborted"])
        self.assertEqual(len(s["lines"]), 1)
        self.assertEqual((s["lines"][0]["ok"], s["lines"][0]["stage"]), (False, "aborted"))
        self.assertGreater(s["cost_cny_total"], 0)
        self.assertAlmostEqual(s["cost_cny_total"], s["spent_cny_total"], places=6)
        self.assertAlmostEqual(sum(c["cost_cny"] for c in self.calls(run_dir)), s["spent_cny_total"], places=6)

    def test_only_one_speaker(self):
        code, run_dir, s, _ = self.run_tts(tts.Settings("voice-x", {"char_luchen": "m"}, only="char_luchen", rounds=1))
        self.assertEqual(code, 0)
        self.assertEqual(s["n_lines"], 5)
        self.assertEqual(sorted(p.stem for p in (run_dir / "samples" / "r1").glob("*.mp3")), ["l_0003", "l_0005", "l_0011", "l_0013", "l_0014"])

    def test_cost_limit_aborts(self):
        code, _, s, out = self.run_tts(tts.Settings("A", VOICES, rounds=1, max_cost_cny=0.001))
        self.assertEqual(code, 1)
        self.assertIsNotNone(s["aborted"])
        self.assertIn("中止", out)

    def test_config_errors(self):
        out = io.StringIO()
        self.assertEqual(tts.run_tts(tts.Settings("A", VOICES), env={}, base_dir=self.base, out=out), 2)
        self.assertEqual(tts.run_tts(tts.Settings("A", {"char_suwan": "x"}), env=ENV, base_dir=self.base, out=out), 2)
        self.assertEqual(tts.run_tts(tts.Settings("A", VOICES, only="char_nobody"), env=ENV, base_dir=self.base, out=out), 2)
        self.assertFalse(any(self.base.iterdir()))  # 配置错误不创建运行目录

    def test_export_and_metrics_reproduce_summary(self):
        asr = FakeASR()
        code, run_dir, s, _ = self.run_tts(tts.Settings("A", VOICES, rounds=1), asr=asr)
        self.assertEqual(code, 0)
        dest = self.base / "export" / "A"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tts.export_run(run_dir, dest), 0)
        self.assertEqual(len(list(dest.glob("l_*.mp3"))), 15)
        self.assertTrue((dest / "run-summary.json").exists() and (dest / "run-calls.jsonl").exists())
        total = sum(x["metrics"]["file_s"] for x in s["lines"])
        self.assertAlmostEqual(tts.audio.mp3_info((dest / "ep01.mp3").read_bytes()).duration_s, total, places=3)
        res = tts.metrics_for_dir(dest)
        self.assertEqual(res["problems"], [])
        by_id = {x["line_id"]: x["metrics"] for x in s["lines"]}
        for m in res["lines"]:
            for k in ("file_s", "effective_s", "cer", "cer_raw", "cer_equiv", "sha256"):
                self.assertEqual(m[k], by_id[m["line_id"]][k], (m["line_id"], k))
        self.assertEqual(res["stats"], s["rounds"][0]["stats"])
        out = io.StringIO()
        self.assertEqual(tts.run_metrics(dest, out=out), 0)
        self.assertIn("l_0015", out.getvalue())
        # 目标目录非空时拒绝覆盖
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tts.export_run(run_dir, dest), 2)

    def test_metrics_reports_problems(self):
        d = self.base / "m"
        d.mkdir()
        (d / "l_0001.mp3").write_bytes(fake_mp3("abc"))
        (d / "l_9999.mp3").write_bytes(fake_mp3("abc"))
        (d / "l_0002.mp3").write_bytes(b"not mp3")
        out = io.StringIO()
        self.assertEqual(tts.run_metrics(d, out=out), 1)
        text = out.getvalue()
        self.assertIn("缺少 asr/l_0001.json", text)
        self.assertIn("l_9999.mp3：不是 ep01 的 line_id", text)
        self.assertIn("l_0002.mp3", text)

    def test_asr_mismatch_counts_cer(self):
        asr = FakeASR(override={"rid-13": "保安，把他带出去。"})
        _, _, s, _ = self.run_tts(tts.Settings("A", VOICES, rounds=1), asr=asr)
        m = [x for x in s["lines"] if x["line_id"] == "l_0014"][0]["metrics"]
        self.assertEqual((m["cer"], m["cer_equiv"]), (0.143, 0.0))
        self.assertEqual(s["cer_nonzero"], 1)
        self.assertEqual(s["cer_equiv_nonzero"], 0)


class CLITest(unittest.TestCase):
    def cli(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["tts", *argv])
        return cm.exception.code, err.getvalue()

    def test_offline_modes_exclude_generation_options(self):
        code, err = self.cli("--metrics", "x", "--voice", "a=b")
        self.assertEqual(code, 2)
        self.assertIn("--voice", err)
        self.assertEqual(self.cli("--metrics", "x", "--rounds", "1")[0], 2)
        self.assertEqual(self.cli("--export", "a", "b", "--instruct")[0], 2)
        self.assertEqual(self.cli("--metrics", "x", "--export", "a", "b")[0], 2)
        self.assertEqual(self.cli("--metrics", "", "--voice", "a=b", "--name", "n")[0], 2)  # 空字符串也算离线模式
        self.assertEqual(self.cli("--metrics", "x", "--rounds", "0")[0], 2)

    def test_json_requires_metrics(self):
        self.assertEqual(self.cli("--json", "--name", "a")[0], 2)

    def test_generation_requires_name_and_valid_voice(self):
        self.assertEqual(self.cli("--voice", "a=b")[0], 2)
        self.assertEqual(self.cli("--name", "a", "--voice", "novalue")[0], 2)


if __name__ == "__main__":
    unittest.main()
