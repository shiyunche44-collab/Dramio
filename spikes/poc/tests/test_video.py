import json
import tempfile
import unittest
from pathlib import Path

from poc import seedance, video


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


if __name__ == "__main__":
    unittest.main()
