import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from poc import compose, media, subtitles


def fake_probe(duration=5.1667, audio=True):
    return lambda path: SimpleNamespace(duration_s=duration, has_audio=audio)


class PlanTest(unittest.TestCase):
    def plan(self, videos: Path, **kw):
        return compose.plan(videos_dir=videos, **kw)

    def test_no_videos_means_all_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            p = self.plan(Path(td))
        self.assertEqual(len(p.shots), 13)
        self.assertTrue(all(s.placeholder and s.video is None for s in p.shots))
        self.assertTrue(all(s.keyframe_sha256 for s in p.shots))

    def test_real_clip_is_chosen_per_shot_with_sha256(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ep01_sc01_sh02.mp4").write_bytes(b"clip")
            p = self.plan(Path(td), probe=fake_probe())
        by = {s.shot_id: s for s in p.shots}
        self.assertEqual(by["ep01_sc01_sh02"].source, "real")
        self.assertEqual(len(by["ep01_sc01_sh02"].video_sha256), 64)
        self.assertEqual(sum(not s.placeholder for s in p.shots), 1)

    def test_timeline_does_not_depend_on_real_or_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            placeholders = self.plan(Path(td))
            for s in placeholders.shots:
                (Path(td) / f"{s.shot_id}.mp4").write_bytes(b"x")
            real = self.plan(Path(td), probe=fake_probe(duration=7.5))
        self.assertEqual([s.target_frames for s in placeholders.shots], [s.target_frames for s in real.shots])
        self.assertEqual([s.start_frames for s in placeholders.shots], [s.start_frames for s in real.shots])
        self.assertEqual(placeholders.total_frames, real.total_frames)

    def test_unparseable_clip_is_an_error_not_a_silent_placeholder(self):
        def bad(path):
            raise media.MediaError("损坏")

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ep01_sc01_sh01.mp4").write_bytes(b"x")
            with self.assertRaises(compose.ComposeError):
                self.plan(Path(td), probe=bad)

    def test_extension_over_limit_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "ep01_sc01_sh05.mp4").write_bytes(b"x")
            with self.assertRaises(compose.ComposeError) as ctx:
                self.plan(Path(td), probe=fake_probe(duration=3.0))
        self.assertIn("延长上限", str(ctx.exception))

    def test_dialogue_windows_follow_asr_and_fit_in_shot(self):
        with tempfile.TemporaryDirectory() as td:
            p = self.plan(Path(td))
        for s in p.shots:
            self.assertGreaterEqual(s.target_s, s.hint_s)
            end = 0.0
            for ln in s.lines:
                self.assertGreaterEqual(ln.start_s, compose.HEAD_S - 1e-6)
                self.assertGreaterEqual(ln.start_s, end)  # 句间不重叠
                end = ln.start_s + ln.dur_s
                self.assertLessEqual(ln.src_out_s, ln.file_s + 1e-6)
            self.assertLessEqual(end, s.target_s + 1e-6)
        self.assertEqual(sum(len(s.lines) for s in p.shots), 15)

    def test_table_and_manifest_entries(self):
        with tempfile.TemporaryDirectory() as td:
            p = self.plan(Path(td))
        table = compose.render_table(p)
        self.assertIn("placeholder 13", table)
        self.assertEqual(len(compose.manifest_entries(p)), 13)


class SubtitleTest(unittest.TestCase):
    def test_wrap(self):
        self.assertEqual(subtitles.wrap("我以为，努力就够了。"), ["我以为，努力就够了。"])
        two = subtitles.wrap("陆总监，这段监控录像，要不要当众放给大家看看？")
        self.assertEqual(len(two), 2)
        self.assertEqual("".join(two), "陆总监，这段监控录像，要不要当众放给大家看看？")
        self.assertTrue(all(len(x) <= subtitles.MAX_CHARS for x in two))

    def test_one_cue_per_line_inside_its_shot(self):
        with tempfile.TemporaryDirectory() as td:
            p = compose.plan(videos_dir=Path(td))
        cues = subtitles.cues(p)
        self.assertEqual(len(cues), 15)
        windows = {s.shot_id: (s.start_frames / compose.FPS, (s.start_frames + s.target_frames) / compose.FPS) for s in p.shots}
        for c in cues:
            lo, hi = windows[c.shot_id]
            self.assertTrue(lo <= c.start_s < c.end_s <= hi + 1e-6)

    def test_ass_text_cannot_inject_override_tags(self):
        self.assertNotIn("{", subtitles.ass_text("a{\\an8}b"))
        self.assertNotIn("}", subtitles.ass_text("a{\\an8}b"))
        self.assertNotIn("\\", subtitles.ass_text("a\\Nb"))
        with self.assertRaises(ValueError):
            subtitles.wrap("字" * 30)

    def test_ass_and_srt(self):
        with tempfile.TemporaryDirectory() as td:
            cues = subtitles.cues(compose.plan(videos_dir=Path(td)))
        ass, srt = subtitles.ass(cues), subtitles.srt(cues)
        self.assertEqual(ass.count("Dialogue: 0,"), 15)
        self.assertIn("Style: Voiceover", ass)
        self.assertEqual(srt.count(" --> "), 15)
        box = subtitles.box(cues[0])
        self.assertEqual(box["y1"], compose.HEIGHT - subtitles.MARGIN_V)
        self.assertGreater(box["x1"], box["x0"])


if __name__ == "__main__":
    unittest.main()
