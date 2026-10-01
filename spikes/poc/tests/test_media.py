import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from poc import media


def make_video(path: Path, seconds: float = 3, size: str = "144x256", rate: int = 24, source: str = "testsrc", audio: bool = True) -> Path:
    """ffmpeg lavfi 合成视频（testsrc 有持续运动，color 静止）；不联网。"""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"{source}=size={size}:rate={rate}:duration={seconds}"]
    if source == "color":
        cmd[-1] = f"color=c=red:size={size}:rate={rate}:duration={seconds}"
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
    subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
    return path


@unittest.skipUnless(media.available(), "没有 ffmpeg / ffprobe")
class MediaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.video = make_video(cls.dir / "t.mp4", seconds=3)
        cls.silent = make_video(cls.dir / "silent.mp4", seconds=2, audio=False)
        cls.still = make_video(cls.dir / "still.mp4", seconds=2, source="color", audio=False)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_probe_actual_values(self):
        info = media.probe(self.video)
        self.assertEqual((info.width, info.height), (144, 256))
        self.assertAlmostEqual(info.duration_s, 3.0, delta=0.1)
        self.assertEqual(info.fps, 24.0)
        self.assertEqual(info.nb_frames, 72)
        self.assertEqual(info.video_codec, "h264")
        self.assertEqual(info.audio_codec, "aac")
        self.assertTrue(info.has_audio)
        self.assertGreater(info.bit_rate, 0)
        self.assertEqual(info.size_bytes, self.video.stat().st_size)
        self.assertIn("mp4", info.container)

    def test_probe_without_audio_stream(self):
        info = media.probe(self.silent)
        self.assertFalse(info.has_audio)
        self.assertIsNone(info.audio_codec)

    def test_probe_rejects_non_media_and_missing(self):
        bad = self.dir / "bad.mp4"
        bad.write_bytes(b"not a video at all")
        with self.assertRaises(media.MediaError):
            media.probe(bad)
        with self.assertRaises(media.MediaError):
            media.probe(self.dir / "missing.mp4")

    def test_frame_indices(self):
        self.assertEqual(media.frame_indices(73), [0, 18, 36, 54, 72])
        self.assertEqual(media.frame_indices(1), [0, 0, 0, 0, 0])
        self.assertEqual(media.frame_indices(2), [0, 0, 1, 1, 1])
        with self.assertRaises(media.MediaError):
            media.frame_indices(0)

    def test_extract_frames_five_positions(self):
        out = self.dir / "frames"
        frames = media.extract_frames(self.video, out)
        self.assertEqual([f.label for f in frames], ["f000", "f025", "f050", "f075", "f100"])
        self.assertEqual([f.index for f in frames], [0, 18, 36, 53, 71])
        for f in frames:
            self.assertTrue(f.path.is_file())
            self.assertEqual(media.image_size(f.path), (144, 256))
        self.assertEqual(len({f.path.read_bytes() for f in frames}), 5)  # testsrc 每帧不同

    def test_ssim_identity_and_difference(self):
        frames = media.extract_frames(self.video, self.dir / "f2")
        first, last = frames[0].path, frames[-1].path
        self.assertAlmostEqual(media.ssim(first, first), 1.0, places=4)
        different = media.ssim(first, last)
        self.assertLess(different, 0.99)
        self.assertGreater(different, 0.0)
        # 参考图比对比图大：先缩放到对比图尺寸再比较
        big = self.dir / "big.jpg"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(first), "-vf", "scale=288:512", str(big)], check=True)
        self.assertGreater(media.ssim(big, first), 0.9)
        self.assertGreater(media.ssim(big, first, size=(144, 256)), 0.9)

    def test_motion_moving_vs_static(self):
        moving, still = media.motion(self.video), media.motion(self.still)
        self.assertEqual(moving.pairs, 71)
        self.assertGreater(moving.mean, 0.05)
        self.assertGreaterEqual(moving.max, moving.p95)
        self.assertGreaterEqual(moving.p95, moving.mean * 0.5)
        self.assertAlmostEqual(still.mean, 0.0, places=3)
        self.assertGreater(moving.mean, still.mean)

    def test_trim_reencodes_to_exact_duration(self):
        src = make_video(self.dir / "five.mp4", seconds=5)
        res = media.trim(src, self.dir / "out" / "four.mp4", 4)
        self.assertTrue(res.reencoded)
        self.assertAlmostEqual(res.source_duration_s, 5.0, delta=0.1)
        out = media.probe(res.path)
        self.assertEqual(out.nb_frames, 96)  # 4 秒 × 24 fps，帧数精确
        self.assertAlmostEqual(out.duration_s, 4.0, delta=0.05)
        self.assertTrue(out.has_audio)
        self.assertEqual((out.width, out.height), (144, 256))
        self.assertEqual(res.as_dict()["reencoded"], True)

    def test_trim_without_audio(self):
        src = make_video(self.dir / "five-silent.mp4", seconds=5, audio=False)
        res = media.trim(src, self.dir / "out" / "four-silent.mp4", 4)
        self.assertTrue(res.reencoded)
        self.assertFalse(media.probe(res.path).has_audio)

    def test_trim_shorter_source_is_copied_not_reencoded(self):
        res = media.trim(self.video, self.dir / "out" / "copy.mp4", 3)  # 源 3 秒 = 目标
        self.assertFalse(res.reencoded)
        self.assertEqual(res.path.read_bytes(), self.video.read_bytes())
        longer = media.trim(self.video, self.dir / "out" / "copy2.mp4", 10)
        self.assertFalse(longer.reencoded)
        with self.assertRaises(media.MediaError):
            media.trim(self.video, self.dir / "out" / "zero.mp4", 0)

    def test_contact_sheet(self):
        frames = media.extract_frames(self.video, self.dir / "f3")
        sheet = media.contact_sheet([f.path for f in frames], self.dir / "sheet" / "strip.jpg", tile_width=72)
        self.assertEqual(media.image_size(sheet), (72 * 5, 128))
        single = media.contact_sheet([frames[0].path], self.dir / "sheet" / "one.jpg", tile_width=72)
        self.assertEqual(media.image_size(single), (72, 128))
        with self.assertRaises(media.MediaError):
            media.contact_sheet([], self.dir / "sheet" / "none.jpg")


class NoFfmpegTest(unittest.TestCase):
    def test_missing_binaries_raise_media_error(self):
        from unittest import mock

        with mock.patch.object(media, "FFPROBE", "ffprobe-does-not-exist"):
            path = Path(tempfile.mkdtemp()) / "x.mp4"
            path.write_bytes(b"x")
            try:
                with self.assertRaises(media.MediaError) as cm:
                    media.probe(path)
                self.assertIn("ffmpeg", str(cm.exception))
            finally:
                shutil.rmtree(path.parent)
            self.assertFalse(shutil.which("ffprobe-does-not-exist"))


class PureFunctionTest(unittest.TestCase):
    def test_rate_and_number_parsing(self):
        self.assertEqual(media._rate("24/1"), 24.0)
        self.assertEqual(media._rate("30000/1001"), 29.97)
        self.assertIsNone(media._rate("0/0"))
        self.assertIsNone(media._rate(None))
        self.assertIsNone(media._float("nan"))
        self.assertEqual(media._int("72"), 72)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(media.available(), "没有 ffmpeg / ffprobe")
class AudioProbeTest(unittest.TestCase):
    def test_probe_audio_loudness_and_tags(self):
        with tempfile.TemporaryDirectory() as td:
            clip = make_video(Path(td) / "a.mp4", seconds=2)
            info = media.probe_audio(clip)
            self.assertEqual((info.codec, info.channels), ("aac", 1))  # 夹具的 sine 是单声道
            silent = make_video(Path(td) / "n.mp4", seconds=1, audio=False)
            self.assertIsNone(media.probe_audio(silent))
            loud = media.loudness(clip)
            self.assertIsNotNone(loud.integrated_lufs)
            self.assertLess(loud.integrated_lufs, 0)
            self.assertEqual(media.format_tags(clip).get("aigc"), None)
            tagged = Path(td) / "t.mp4"
            media._run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(clip), "-c", "copy", "-metadata", "AIGC={\"Label\":\"1\"}",
                        "-movflags", "+use_metadata_tags", str(tagged)])
            self.assertEqual(media.format_tags(tagged)["aigc"], '{"Label":"1"}')
