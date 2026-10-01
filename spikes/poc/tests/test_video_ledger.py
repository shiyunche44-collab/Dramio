import json
import tempfile
import unittest
from pathlib import Path

from poc import media, video, video_ledger
from tests.test_media import make_video


@unittest.skipUnless(media.available(), "没有 ffmpeg / ffprobe")
class LedgerVerifyTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name) / "run"
        (self.dir / "videos").mkdir(parents=True)
        path = make_video(self.dir / "videos" / "s1.mp4", seconds=1)
        info = media.probe(path)
        self.ledger = {
            "tasks": [{
                "shot_id": "s1", "task_id": "1", "node_key": "k1", "model": "MiniMax-H3", "resolution": "768P", "requested_duration_s": 1,
                "status": "succeeded", "usage": {"output_seconds": 1}, "latency_s": 5, "cost_cny": 0.5, "cost_basis": "usage",
                "video": {"path": "videos/s1.mp4", "bytes": path.stat().st_size, "sha256": video_ledger._sha256(path),
                          "width": info.width, "height": info.height, "duration_s": info.duration_s, "fps": info.fps,
                          "has_audio": info.has_audio, "sha256_matches_remote": True},
            }],
            "totals": {"tasks": 1, "succeeded": 1, "output_seconds": 1, "cost_cny": 0.5, "latency_s": None},
        }
        self._write()

    def tearDown(self):
        self._td.cleanup()

    def _write(self):
        (self.dir / video_ledger.LEDGER).write_text(json.dumps(self.ledger), encoding="utf-8")

    def test_consistent_ledger_passes(self):
        self.assertEqual(video.verify(self.dir), (True, []))

    def test_parent_directory_finds_nested_ledgers(self):
        self.assertTrue(video.verify(self.dir.parent)[0])

    def test_tampered_video_fails_sha256(self):
        with (self.dir / "videos" / "s1.mp4").open("ab") as f:
            f.write(b"x")
        ok, errors = video.verify(self.dir)
        self.assertFalse(ok)
        self.assertTrue(any("sha256" in e for e in errors))

    def test_cost_total_must_match_tasks(self):
        self.ledger["totals"]["cost_cny"] = 9.0
        self._write()
        ok, errors = video.verify(self.dir)
        self.assertFalse(ok)
        self.assertTrue(any("费用合计" in e for e in errors))

    def test_remote_mismatch_is_reported(self):
        self.ledger["tasks"][0]["video"]["sha256_matches_remote"] = False
        self._write()
        self.assertTrue(any("错位" in e for e in video.verify(self.dir)[1]))

    def test_signed_url_and_bearer_are_rejected(self):
        (self.dir / "note.json").write_text('{"u": "https://h/x.mp4?Signature=abc&Expires=1"}', encoding="utf-8")
        (self.dir / "note2.md").write_text("Authorization: Bearer abcdefghijklmnop1234", encoding="utf-8")
        errors = video.verify(self.dir)[1]
        self.assertTrue(any("带签名" in e for e in errors))
        self.assertTrue(any("Bearer" in e for e in errors))

    def test_unverified_remote_and_orphan_video_are_reported(self):
        self.ledger["tasks"][0]["video"]["sha256_matches_remote"] = None
        self._write()
        make_video(self.dir / "videos" / "stray.mp4", seconds=1)
        errors = video.verify(self.dir)[1]
        self.assertTrue(any("没有与供应商产物比对" in e for e in errors))
        self.assertTrue(any("stray.mp4" in e and "不在账本里" in e for e in errors))

    def test_size_limit(self):
        ok, errors = video.verify(self.dir, max_bytes=10)
        self.assertFalse(ok)
        self.assertTrue(any("体积" in e for e in errors))

    def test_size_counts_files_without_git(self):
        self.assertGreater(video_ledger.du_bytes(self.dir), 0)


if __name__ == "__main__":
    unittest.main()
