import json
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from poc import minimax, seedance, video


class VideoPlanTest(unittest.TestCase):
    def test_fixed_manifest_produces_thirteen_jobs(self):
        jobs = video.plan()
        self.assertEqual(len(jobs), 13)
        self.assertEqual(sum(j.duration for j in jobs), 60)
        self.assertEqual(len({j.node_key for j in jobs}), 13)
        self.assertTrue(all(len(j.image_sha256) == 64 for j in jobs))

    def test_dry_run_is_offline_and_costed(self):
        jobs = video.plan(shots=["ep01_sc01_sh02"])
        with tempfile.TemporaryDirectory() as td:
            result = video.run_jobs(jobs, Path(td), dry_run=True, max_cost_cny=5)
        self.assertEqual(result["jobs"][0]["shot_id"], "ep01_sc01_sh02")
        self.assertAlmostEqual(result["estimated_cost_cny"], 2.5)
        self.assertFalse(Path(td).exists())

    def test_verify_rejects_missing_summary(self):
        with tempfile.TemporaryDirectory() as td:
            ok, errors = video.verify(Path(td))
        self.assertFalse(ok)
        self.assertIn("run-summary", errors[0])

    def test_ark_cost_uses_output_resolution_not_keyframe_pixels(self):
        job = video.plan(candidate="2.0-mini", resolution="480p", shots=["ep01_sc01_sh01"])[0]
        self.assertLess(video._cost(job).cny, 1.5)

    def test_plan_billing_uses_undated_model_alias(self):
        job = video.plan(candidate="2.0-mini", resolution="480p", shots=["ep01_sc01_sh01"])[0]
        request = seedance.VideoRequest(job.model, job.resolution.lower(), job.duration, job.prompt, Path(job.image_path).read_bytes())
        self.assertEqual(request.body(billing="plan")["model"], "doubao-seedance-2.0-mini")
        self.assertEqual(request.body()["model"], job.model)


class FakeClient:
    """假 MiniMax 客户端：按需抛提交错误 / 下载错误，记录提交次数。"""

    def __init__(self, submit_error=None, download_error=None):
        self.submit_error, self.download_error, self.submitted = submit_error, download_error, 0

    def submit(self, request):
        self.submitted += 1
        if self.submit_error:
            raise self.submit_error
        return minimax.Submitted("111", None)

    def query(self, task_id, api):
        return minimax.TaskState(task_id, "succeeded", usage={"output_seconds": 5}, video_url="https://example.invalid/v.mp4")

    def download_url(self, state, api):
        return state.video_url

    def download(self, url):
        if self.download_error:
            raise self.download_error
        return b"fake"


class RunJobsTest(unittest.TestCase):
    def _run(self, client, shots, out, resume=False):
        jobs = video.plan(shots=shots)
        with mock.patch.object(video, "_client", return_value=client), mock.patch.object(video.media, "probe", side_effect=RuntimeError("no media")):
            return video.run_jobs(jobs, out, resume=resume, poll_s=0, max_cost_cny=50)

    def test_quota_error_aborts_remaining_shots(self):
        err = minimax.VideoError("rate_limit", "HTTP 429 已达到 Token Plan 用量上限 (2056)")
        client = FakeClient(submit_error=err)
        with tempfile.TemporaryDirectory() as td:
            summary = self._run(client, ["ep01_sc01_sh01", "ep01_sc01_sh02", "ep01_sc01_sh03"], Path(td))
        self.assertEqual(client.submitted, 1)
        self.assertEqual(summary["jobs"][-1]["status"], "aborted")

    def test_cost_is_recorded_before_download(self):
        client = FakeClient(download_error=minimax.VideoError("network", "reset", transient=True))
        with tempfile.TemporaryDirectory() as td:
            self._run(client, ["ep01_sc01_sh02"], Path(td))
            calls = [json.loads(line) for run in (Path(td) / "runs").glob("*/calls.jsonl") for line in run.read_text().splitlines()]
        self.assertEqual(calls[0]["cost_cny"], 2.5)
        self.assertEqual(calls[0]["cost_basis"], "estimate")

    def test_resume_reuses_task_without_resubmitting_or_duplicating(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            first = FakeClient(download_error=minimax.VideoError("network", "reset", transient=True))
            self._run(first, ["ep01_sc01_sh02"], out)
            second = FakeClient(download_error=minimax.VideoError("network", "reset", transient=True))
            self._run(second, ["ep01_sc01_sh02"], out, resume=True)
            lines = (out / "tasks.jsonl").read_text().splitlines()
            calls = [json.loads(line) for run in sorted((out / "runs").glob("*/calls.jsonl")) for line in run.read_text().splitlines()]
        self.assertEqual((first.submitted, second.submitted), (1, 0))
        self.assertEqual(len(lines), 1)
        self.assertEqual(sorted(c["cost_basis"] for c in calls), ["estimate", "free"])  # 沿用旧任务不重复计费（run_id 同秒时顺序不定，所以不按位置断言）

    def test_failed_task_is_marked_retryable_and_resubmitted(self):
        class Failing(FakeClient):
            def query(self, task_id, api):
                return minimax.TaskState(task_id, "failed", error_code="1000", error_message="internal")

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._run(Failing(), ["ep01_sc01_sh02"], out)
            record = json.loads((out / "tasks.jsonl").read_text().splitlines()[-1])
            again = FakeClient()
            self._run(again, ["ep01_sc01_sh02"], out, resume=True)
        self.assertTrue(record["retry"])
        self.assertEqual(again.submitted, 1)


if __name__ == "__main__":
    unittest.main()
