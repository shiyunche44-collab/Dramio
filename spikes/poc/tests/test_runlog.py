import json
import os
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from poc import runlog


class RunlogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_run_id_format(self):
        self.assertRegex(runlog.new_run_id("doctor"), r"^\d{8}-\d{6}-doctor-[0-9a-f]{4}$")

    def test_default_runs_dir_is_package_relative(self):
        env = {k: v for k, v in os.environ.items() if k != "POC_RUNS_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(runlog.runs_dir(), Path(runlog.config.PROJECT_DIR) / "runs")

    def test_run_directory_and_meta(self):
        with runlog.Run("demo", {"x": 1}, base_dir=self.base) as run:
            pass
        self.assertEqual(run.dir.parent, self.base)
        self.assertTrue((run.dir / "calls.jsonl").is_file())
        meta = json.loads((run.dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["command"], "demo")
        self.assertEqual(meta["args"], {"x": 1})
        self.assertEqual(meta["status"], "ok")
        self.assertIsNotNone(meta["ended_at"])

    def test_calls_jsonl_success_and_failure(self):
        run = runlog.Run("demo", base_dir=self.base)
        with run.call(provider="p1", capability="llm", model="m1", node_key="nk") as call:
            call.cost_cny = 0.12
            call.cost_basis = "estimate"
            call.usage = {"input_tokens": 10}
        with self.assertRaises(RuntimeError) as ctx:
            with run.call(provider="p2", capability="tts"):
                raise RuntimeError("boom")
        self.assertEqual(str(ctx.exception), "boom")

        lines = run.calls_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        ok, err = (json.loads(line) for line in lines)
        for row in (ok, err):
            for key in ("run_id", "provider", "capability", "model", "elapsed_ms", "cost_cny", "cost_basis", "status"):
                self.assertIn(key, row)
            self.assertEqual(row["run_id"], run.run_id)
            self.assertGreaterEqual(row["elapsed_ms"], 0)
        self.assertEqual((ok["status"], ok["cost_cny"], ok["model"], ok["node_key"]), ("ok", 0.12, "m1", "nk"))
        self.assertEqual(err["status"], "error")
        self.assertEqual(err["error"], "RuntimeError: boom")

    def test_non_serializable_fields_do_not_mask_exception(self):
        run = runlog.Run("demo", base_dir=self.base)
        with self.assertRaises(KeyError):
            with run.call(provider="p", capability="llm") as call:
                call.usage = {"at": datetime(2026, 1, 1), "obj": object()}
                raise KeyError("original")
        cyclic: dict = {}
        cyclic["self"] = cyclic
        with run.call(provider="p", capability="llm") as call:
            call.extra = cyclic
        rows = [json.loads(line) for line in run.calls_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(rows[0]["usage"]["at"], "2026-01-01 00:00:00")
        self.assertIn("serialize_error", rows[1])

    def test_explicit_secrets_are_redacted(self):
        run = runlog.Run("demo", base_dir=self.base, secrets=["sk-SECRET-123\n"])
        with self.assertRaises(ValueError):
            with run.call(provider="p", capability="llm"):
                raise ValueError(repr("sk-SECRET-123\n"))
        self.assertNotIn("SECRET", run.calls_path.read_text(encoding="utf-8"))

    def test_write_json_is_redacted(self):
        run = runlog.Run("demo", {"note": "key sk-SECRET-456"}, base_dir=self.base, secrets=["sk-SECRET-456"])
        run.write_json("summary.json", {"detail": "HTTP 401 sk-SECRET-456"})
        run.finish()
        for name in ("meta.json", "summary.json"):
            self.assertNotIn("SECRET", (run.dir / name).read_text(encoding="utf-8"), name)
        self.assertEqual(json.loads((run.dir / "summary.json").read_text(encoding="utf-8")), {"detail": "HTTP 401 ***"})

    def test_run_dir_env_override(self):
        with mock.patch.dict(os.environ, {"POC_RUNS_DIR": str(self.base / "custom")}):
            run = runlog.Run("demo")
        self.assertEqual(run.dir.parent, self.base / "custom")
        self.assertTrue(re.match(r"^\d{8}-", run.run_id))
