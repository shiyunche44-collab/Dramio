import tempfile
import unittest
from pathlib import Path

from poc import compose_cmd


class VerifyTest(unittest.TestCase):
    def test_missing_manifest_and_video_fail(self):
        with tempfile.TemporaryDirectory() as td:
            ok, errors, _ = compose_cmd.verify(Path(td))
            self.assertFalse(ok)
            self.assertIn("compose-manifest", errors[0])
            (Path(td) / "compose-manifest.json").write_text("{}", encoding="utf-8")
            ok, errors, _ = compose_cmd.verify(Path(td))
            self.assertFalse(ok)
            self.assertIn("final.mp4", errors[0])

    def test_dry_run_writes_nothing(self):
        import io
        from poc import __main__ as cli
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "never"
            args = cli.build_parser().parse_args(["compose", "--dry-run", "--export", str(out), "--videos", str(Path(td) / "v")])
            buf = io.StringIO()
            self.assertEqual(compose_cmd.run_compose(args, buf), 0)
            self.assertFalse(out.exists())
            self.assertIn("placeholder 13", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
