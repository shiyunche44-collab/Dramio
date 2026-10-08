import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from poc import compose, lipsync, media, minimax


def _line(start=0.15, dur=1.5, text="你好"):
    return compose.LinePlan("l_x", "char_suwan", "dialogue", text, "docs/reports/p0/P0-05/C-instruct-speed/l_0004.mp3", "0" * 64, 0.1, 0.1 + dur, start, start + 0.15, start + dur - 0.25, 2.4)


@dataclass
class _Info:
    duration_s: float = 5.0


class FakeClient:
    def __init__(self, submit_error=None):
        self.submit_error, self.submitted, self.bodies = submit_error, 0, []

    def submit_raw(self, method, path, body):
        self.submitted += 1
        self.bodies.append((method, path, body))
        if self.submit_error:
            raise self.submit_error
        return minimax.Submitted("222", None)

    def query(self, task_id, api):
        return minimax.TaskState(task_id, "succeeded", usage={"output_seconds": 5}, video_url="https://example.invalid/v.mp4")

    def download_url(self, state, api):
        return state.video_url

    def download(self, url):
        return b"mp4-bytes"


class AudioPlanTest(unittest.TestCase):
    def test_audio_length_pads_to_two_seconds(self):
        self.assertEqual(lipsync.audio_length([_line(dur=1.0)]), 2.0)

    def test_audio_length_follows_last_line_plus_tail(self):
        self.assertAlmostEqual(lipsync.audio_length([_line(start=0.15, dur=3.0), _line(start=3.35, dur=2.0)]), 5.35 + compose.TAIL_S)

    def test_audio_longer_than_fifteen_seconds_is_rejected(self):
        with self.assertRaises(lipsync.LipsyncError):
            lipsync.audio_length([_line(start=0.15, dur=15.0)])

    def test_no_dialogue_is_rejected(self):
        with self.assertRaises(lipsync.LipsyncError):
            lipsync.audio_length([])

    def test_filter_delays_each_line_to_its_shot_offset(self):
        inputs, graph = lipsync.audio_filter([_line(start=0.15), _line(start=2.0)], 4.0)
        self.assertEqual(len(inputs), 2)
        self.assertIn("adelay=150:all=1", graph)
        self.assertIn("adelay=2000:all=1", graph)
        self.assertIn("amix=inputs=2:normalize=0", graph)

    def test_duration_for_is_integer_within_h3_range(self):
        self.assertEqual(lipsync.duration_for(4, 2.4), 4)
        self.assertEqual(lipsync.duration_for(5, 2.4), 5)
        self.assertEqual(lipsync.duration_for(5, 7.15), 8)
        self.assertEqual(lipsync.duration_for(3, 2.0), 4)
        self.assertEqual(lipsync.duration_for(5, 20), 15)


class RequestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.jobs = lipsync.plan(shots=["ep01_sc01_sh04"], out=Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def job(self, route):
        return next(j for j in self.jobs if j.route == route)

    def media(self, job):
        return lipsync._load_media(job)

    def test_plan_has_one_job_per_route_with_distinct_keys(self):
        self.assertEqual([j.route for j in self.jobs], ["A", "B"])
        self.assertEqual(len({j.node_key for j in self.jobs}), 2)

    def test_route_a_uses_first_frame_only(self):
        job = self.job("A")
        roles = [c.get("role") for c in lipsync.body(job, *self.media(job))["content"]]
        self.assertEqual(roles, [None, "first_frame"])

    def test_route_b_uses_reference_roles_and_never_first_frame(self):
        job = self.job("B")
        content = lipsync.body(job, *self.media(job))["content"]
        self.assertEqual([c.get("role") for c in content], [None, "reference_image", "reference_audio"])
        self.assertEqual(content[2]["type"], "audio_url")
        self.assertTrue(content[2]["audio_url"]["url"].startswith("data:audio/mp3;base64,"))

    def test_prompts_carry_dialogue_and_names_not_ids(self):
        for route in ("A", "B"):
            prompt = self.job(route).prompt
            self.assertIn("那是我熬了三个月的方案。", prompt)
            self.assertNotIn("char_suwan", prompt)
        self.assertIn("音色参考音频1", self.job("B").prompt)
        self.assertNotIn("音色参考音频1", self.job("A").prompt)

    def test_preview_has_no_base64_and_is_validated(self):
        text = json.dumps(lipsync.preview(self.jobs), ensure_ascii=False)
        self.assertNotIn(";base64,", text.replace("base64, ", ""))
        self.assertIn("sha256=", text)

    def test_audio_too_short_or_duration_shorter_than_audio_is_rejected(self):
        job = self.job("B")
        image, audio = self.media(job)
        short = lipsync.AudioInput(job.audio.path, job.audio.sha256, 1.0, [], [])
        with self.assertRaises(lipsync.LipsyncError):
            lipsync.validate_job(lipsync.Job(**{**job.__dict__, "audio": short}), image, audio)
        longer = lipsync.AudioInput(job.audio.path, job.audio.sha256, 6.0, [], [])
        with self.assertRaises(lipsync.LipsyncError):
            lipsync.validate_job(lipsync.Job(**{**job.__dict__, "audio": longer}), image, audio)

    def test_route_b_without_audio_is_rejected(self):
        job = self.job("B")
        image, _ = self.media(job)
        with self.assertRaises(lipsync.LipsyncError):
            lipsync.validate_job(lipsync.Job(**{**job.__dict__, "audio": None}), image, None)

    def test_shots_outside_scope_are_rejected(self):
        with self.assertRaises(lipsync.LipsyncError):
            lipsync.plan(shots=["ep01_sc02_sh03"], build=False)  # D-013：没有 H3 片段
        with self.assertRaises(lipsync.LipsyncError):
            lipsync.plan(routes=("C",), build=False)

    def test_cost_guard_blocks_before_any_submit(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as td, self.assertRaises(lipsync.LipsyncError):
            lipsync.run_jobs(self.jobs, Path(td), max_cost_cny=1.0, client=client)
        self.assertEqual(client.submitted, 0)


class RunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.jobs = lipsync.plan(routes=("A",), shots=["ep01_sc01_sh04"], out=Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_it(self, out, client, **kw):
        with mock.patch.object(lipsync.media, "probe", return_value=_Info()):
            return lipsync.run_jobs(self.jobs, out, client=client, sleep=lambda s: None, max_cost_cny=10, **kw)

    def test_submit_is_recorded_immediately_and_summaries_accumulate(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            result = self.run_it(out, client)
            self.assertEqual(result["success"], 1)
            lines = [json.loads(x) for x in (out / "tasks.jsonl").read_text().splitlines()]
            self.assertEqual([x["status"] for x in lines], ["submitted", "succeeded"])
            self.assertTrue((out / "videos" / "A_ep01_sc01_sh04.mp4").exists())
            self.assertEqual(client.bodies[0][:2], ("POST", "/v2/video_generation"))
            # 默认读 tasks.jsonl 去重：再跑一次不重新提交；--force（resume=False）才重提
            self.run_it(out, client)
            self.assertEqual(client.submitted, 1)
            self.run_it(out, client, resume=False)
            self.assertEqual(client.submitted, 2)
            self.assertEqual(json.loads((out / "run-summary.json").read_text())["count"], 1)

    def test_quota_error_aborts_the_run(self):
        err = minimax.VideoError("rate_limit", "HTTP 429 code=2056 已达到用量上限", http_status=429, api_code="2056")
        with tempfile.TemporaryDirectory() as td:
            result = self.run_it(Path(td), FakeClient(submit_error=err))
        self.assertEqual(result["success"], 0)
        self.assertEqual(result["jobs"][-1]["status"], "aborted")
        self.assertNotIn("task_id", json.dumps(result["jobs"][0]))


if __name__ == "__main__":
    unittest.main()


class VerifyAndReviewTest(unittest.TestCase):
    def test_verify_flags_missing_files_and_cost_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "videos").mkdir()
            (out / "runs" / "r1").mkdir(parents=True)
            rec = {"route": "A", "shot_id": "ep01_sc01_sh04", "node_key": "k1", "task_id": "t1", "status": "succeeded", "bytes": 3, "cost_cny": 2.5}
            (out / "tasks.jsonl").write_text(json.dumps({**rec, "status": "succeeded"}) + "\n")
            (out / "run-summary.json").write_text(json.dumps({"jobs": [rec]}))
            (out / "runs" / "r1" / "calls.jsonl").write_text(json.dumps({"cost_cny": 1.0}) + "\n")
            ok, errors = lipsync.verify(out)
            self.assertFalse(ok)
            self.assertTrue(any("缺少视频文件" in e for e in errors))
            self.assertTrue(any("费用不一致" in e for e in errors))
            (out / "videos" / "A_ep01_sc01_sh04.mp4").write_bytes(b"abc")
            (out / "runs" / "r1" / "calls.jsonl").write_text(json.dumps({"cost_cny": 2.5}) + "\n")
            ok, errors = lipsync.verify(out)
            self.assertTrue(ok, errors)

    def test_verify_flags_orphan_video_and_signed_urls(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "videos").mkdir()
            (out / "runs").mkdir()
            (out / "tasks.jsonl").write_text("")
            (out / "run-summary.json").write_text(json.dumps({"jobs": []}))
            (out / "videos" / "B_ep01_sc01_sh03.mp4").write_bytes(b"x")
            (out / "note.json").write_text('{"u": "https://example.com/a.mp4?Signature=abc&Expires=1"}')
            ok, errors = lipsync.verify(out)
            self.assertFalse(ok)
            self.assertTrue(any("没有对应的成功任务" in e for e in errors))
            self.assertTrue(any("签名" in e for e in errors))
