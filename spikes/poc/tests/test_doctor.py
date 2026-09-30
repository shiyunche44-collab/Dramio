import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from poc import config, doctor, providers

SENTINEL = "sk-SENTINEL-7f3a9c2e5b1d4a68"
POC_DIR = config.PROJECT_DIR
REPO_ROOT = POC_DIR.parent.parent


class FakeResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int) -> HTTPError:
    return HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(b""))


class DoctorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def doctor(self, env, **kw):
        out = io.StringIO()
        code = doctor.run_doctor(env=env, out=out, base_dir=self.base, **kw)
        (run_dir,) = self.base.iterdir()
        return code, out.getvalue(), run_dir

    def results(self, run_dir):
        data = json.loads((run_dir / "doctor.json").read_text(encoding="utf-8"))
        return {r["provider"]: r for r in data["results"]}


class NoKeysTest(DoctorTestCase):
    def test_all_missing_exit_zero(self):
        with mock.patch("poc.doctor.urlopen") as urlopen:
            code, out, run_dir = self.doctor({})
        urlopen.assert_not_called()
        self.assertEqual(code, 0)
        results = self.results(run_dir)
        self.assertEqual(set(results), set(providers.BY_NAME))
        for p in providers.PROVIDERS:
            self.assertEqual(results[p.name]["status"], doctor.MISSING)
            self.assertEqual(results[p.name]["missing"], list(p.env))
            self.assertIn(p.name, out)
            for var in p.env:
                self.assertIn(var, out)
        self.assertIn("能力覆盖", out)
        for f in ("meta.json", "doctor.json", "calls.jsonl"):
            self.assertTrue((run_dir / f).is_file(), f)
        self.assertEqual((run_dir / "calls.jsonl").read_text(), "")

    def test_partially_configured_multi_var_provider_is_missing(self):
        code, _, run_dir = self.doctor({"KLING_ACCESS_KEY": "a"})
        kling = self.results(run_dir)["kling"]
        self.assertEqual((kling["status"], kling["missing"]), (doctor.MISSING, ["KLING_SECRET_KEY"]))


class ClassifyTest(DoctorTestCase):
    def check(self, side_effect, offline=False):
        p = providers.BY_NAME["anthropic"]
        with mock.patch("poc.doctor.urlopen", side_effect=side_effect) as urlopen:
            result = doctor.check_provider(p, {"ANTHROPIC_API_KEY": SENTINEL}, None, offline=offline)
        return result, urlopen

    def test_ok(self):
        self.assertEqual(self.check(lambda *a, **k: FakeResponse(200))[0].status, doctor.OK)

    def test_rate_limited_counts_as_ok(self):
        self.assertEqual(self.check(http_error(429))[0].status, doctor.OK)

    def test_invalid(self):
        for code in (401, 403):
            result = self.check(http_error(code))[0]
            self.assertEqual((result.status, result.http_status), (doctor.INVALID, code))

    def test_google_invalid_key_400(self):
        body = b'{"error": {"code": 400, "details": [{"reason": "API_KEY_INVALID"}]}}'
        err = HTTPError("https://example.invalid", 400, "bad", {}, io.BytesIO(body))
        self.assertEqual(self.check(err)[0].status, doctor.INVALID)
        self.assertEqual(self.check(http_error(400))[0].status, doctor.ERROR)

    def test_redirect_is_error_and_not_followed(self):
        self.assertEqual(self.check(http_error(302))[0].status, doctor.ERROR)
        handler = doctor._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.invalid/"))

    def test_unexpected_exception_is_error_without_message(self):
        result = self.check(ValueError(f"Invalid header value {SENTINEL!r}"))[0]
        self.assertEqual(result.status, doctor.ERROR)
        self.assertEqual(result.detail, "探测异常：ValueError")

    def test_whitespace_only_is_missing_and_padding_is_stripped(self):
        p = providers.BY_NAME["anthropic"]
        with mock.patch("poc.doctor.urlopen") as urlopen:
            result = doctor.check_provider(p, {"ANTHROPIC_API_KEY": " \n"}, None)
        self.assertEqual(result.status, doctor.MISSING)
        urlopen.assert_not_called()
        with mock.patch("poc.doctor.urlopen", return_value=FakeResponse(200)) as urlopen:
            result = doctor.check_provider(p, {"ANTHROPIC_API_KEY": f"  {SENTINEL}\n"}, None)
        self.assertEqual(result.status, doctor.OK)
        self.assertEqual(urlopen.call_args.args[0].get_header("X-api-key"), SENTINEL)

    def test_control_chars_are_invalid_without_network(self):
        p = providers.BY_NAME["anthropic"]
        with mock.patch("poc.doctor.urlopen") as urlopen:
            result = doctor.check_provider(p, {"ANTHROPIC_API_KEY": "sk-a\nb"}, None)
        self.assertEqual(result.status, doctor.INVALID)
        urlopen.assert_not_called()

    def test_server_error(self):
        self.assertEqual(self.check(http_error(503))[0].status, doctor.ERROR)

    def test_unreachable(self):
        for exc in (
            URLError(socket.gaierror("Name or service not known")),
            URLError(OSError("Tunnel connection failed: 403 Forbidden")),
            TimeoutError("timed out"),
            ConnectionRefusedError(),
        ):
            result = self.check(exc)[0]
            self.assertEqual(result.status, doctor.UNREACHABLE, repr(exc))
            self.assertIsNone(result.http_status)

    def test_offline_does_not_touch_network(self):
        result, urlopen = self.check(AssertionError("network used"), offline=True)
        self.assertEqual(result.status, doctor.CONFIGURED)
        urlopen.assert_not_called()

    def test_no_probe_is_configured_without_network(self):
        p = providers.BY_NAME["fal"]
        self.assertIsNone(p.probe)
        with mock.patch("poc.doctor.urlopen") as urlopen:
            result = doctor.check_provider(p, {"FAL_KEY": SENTINEL}, None)
        self.assertEqual(result.status, doctor.CONFIGURED)
        urlopen.assert_not_called()

    def test_probe_request_is_get_with_auth(self):
        _, urlopen = self.check(lambda *a, **k: FakeResponse(200))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/models")
        self.assertEqual(request.get_header("X-api-key"), SENTINEL)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], doctor.DEFAULT_TIMEOUT)


class RequireTest(DoctorTestCase):
    env = {"ANTHROPIC_API_KEY": SENTINEL, "DEEPSEEK_API_KEY": SENTINEL + "2"}

    def fake_urlopen(self, request, timeout):
        if "deepseek" in request.full_url:
            raise http_error(401)
        return FakeResponse(200)

    def test_require_all_ok(self):
        with mock.patch("poc.doctor.urlopen", side_effect=self.fake_urlopen):
            code, out, _ = self.doctor(self.env, require=["anthropic"])
        self.assertEqual(code, 0)

    def test_require_not_ok(self):
        with mock.patch("poc.doctor.urlopen", side_effect=self.fake_urlopen):
            code, out, run_dir = self.doctor(self.env, require=["anthropic", "deepseek"])
        self.assertEqual(code, 1)
        self.assertIn("deepseek", out)
        data = json.loads((run_dir / "doctor.json").read_text(encoding="utf-8"))
        self.assertEqual(data["require_failed"], ["deepseek"])

    def test_require_missing_provider_fails(self):
        with mock.patch("poc.doctor.urlopen", side_effect=self.fake_urlopen):
            code, _, _ = self.doctor({}, require=["anthropic"])
        self.assertEqual(code, 1)

    def test_require_unknown_provider(self):
        out = io.StringIO()
        self.assertEqual(doctor.run_doctor(require=["nope"], env={}, out=out, base_dir=self.base), 2)
        self.assertIn("未知的供应商", out.getvalue())

    def test_probes_are_logged_as_free_calls(self):
        with mock.patch("poc.doctor.urlopen", side_effect=self.fake_urlopen):
            _, _, run_dir = self.doctor(self.env)
        rows = [json.loads(line) for line in (run_dir / "calls.jsonl").read_text().splitlines()]
        self.assertEqual({r["provider"] for r in rows}, {"anthropic", "deepseek"})
        for row in rows:
            self.assertEqual((row["cost_cny"], row["cost_basis"]), (0.0, "free"))
        self.assertEqual({r["provider"]: r["status"] for r in rows}, {"anthropic": "ok", "deepseek": "error"})


class SecretLeakTest(DoctorTestCase):
    def assert_no_leak(self, text: str, where: str):
        # 整个值以及任意 8 个字符的片段都不能出现
        for i in range(len(SENTINEL) - 7):
            self.assertNotIn(SENTINEL[i : i + 8], text, where)

    def test_secret_never_written(self):
        env = {v: SENTINEL for v in providers.all_env_vars()}
        responses = iter([FakeResponse(200), http_error(401), http_error(500), URLError("blocked")] * 5)

        def fake(request, timeout):
            r = next(responses)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch("poc.doctor.urlopen", side_effect=fake):
            _, out, run_dir = self.doctor(env)
        self.assert_no_leak(out, "stdout")
        for f in ("meta.json", "doctor.json", "calls.jsonl"):
            self.assert_no_leak((run_dir / f).read_text(encoding="utf-8"), f)


class CliTest(unittest.TestCase):
    """真实进程：无密钥环境（清空供应商变量、不读 .env），从 spikes/poc 与仓库根目录运行。"""

    def run_cli(self, cwd: Path, runs_dir: Path | None, *args: str, extra_env: dict | None = None):
        env = {k: v for k, v in os.environ.items() if k not in providers.all_env_vars()}
        env.update(extra_env or {})
        env["POC_ENV_FILE"] = "/nonexistent/.env"
        env["PYTHONPATH"] = str(POC_DIR)
        env.pop("POC_RUNS_DIR", None)
        if runs_dir is not None:
            env["POC_RUNS_DIR"] = str(runs_dir)
        return subprocess.run(
            [sys.executable, "-m", "poc", "doctor", *args], cwd=cwd, env=env, capture_output=True, text=True
        )

    def test_doctor_without_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli(POC_DIR, Path(tmp))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.count("MISSING"), len(providers.PROVIDERS))
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def output_dir(self, stdout: str) -> Path:
        (line,) = [l for l in stdout.splitlines() if l.startswith("输出：")]
        path = Path(line[len("输出："):])
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_from_repo_root_writes_under_spikes_poc(self):
        root_runs_existed = (REPO_ROOT / "runs").exists()
        proc = self.run_cli(REPO_ROOT, None, "--offline")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run_dir = self.output_dir(proc.stdout)
        self.assertEqual(run_dir.parent, POC_DIR / "runs")
        self.assertTrue((run_dir / "doctor.json").is_file())
        if not root_runs_existed:
            self.assertFalse((REPO_ROOT / "runs").exists())

    def test_secret_with_newline_never_leaks(self):
        """真实进程 + 真实 urllib：密钥含换行时不能崩溃，也不能出现在任何输出中。"""
        secret = "sk-SENTINEL-7f3a9c2e\nb1d4a68"
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli(POC_DIR, Path(tmp), "--timeout", "0.01", extra_env={"ANTHROPIC_API_KEY": secret})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            (run_dir,) = Path(tmp).iterdir()
            texts = {"stdout": proc.stdout, "stderr": proc.stderr}
            for f in run_dir.iterdir():
                texts[f.name] = f.read_text(encoding="utf-8")
        for where, text in texts.items():
            for fragment in ("SENTINEL", "7f3a9c2e", "b1d4a68"):
                self.assertNotIn(fragment, text, where)
        self.assertIn("INVALID", proc.stdout)

if __name__ == "__main__":
    unittest.main()
