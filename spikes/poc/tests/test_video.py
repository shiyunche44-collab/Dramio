import json
import tempfile
import unittest
from pathlib import Path

from poc import video


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


if __name__ == "__main__":
    unittest.main()
