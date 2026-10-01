import argparse
import inspect
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from poc import compose, compose_cmd, media, otio_timeline, render, subtitles
from tests.test_media import make_video

try:
    import opentimelineio as otio
except ImportError:  # 单元测试不需要它
    otio = None

HAS_FONT = Path(subtitles.FONT_FILE).is_file()


def fake_probe(path):
    return SimpleNamespace(duration_s=5.1667, has_audio=True)


@unittest.skipUnless(otio, "没有安装 opentimelineio（pip install -e '.[otio]'）")
class OtioTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        (self.dir / "videos").mkdir()
        for sid in ("ep01_sc01_sh01", "ep01_sc01_sh05"):
            (self.dir / "videos" / f"{sid}.mp4").write_bytes(sid.encode())
        self.plan = compose.plan(videos_dir=self.dir / "videos", probe=fake_probe)
        self.tl = otio_timeline.build(self.plan, self.dir, "low")

    def tearDown(self):
        self._td.cleanup()

    def test_tracks_and_counts(self):
        tracks = {t.name: t for t in self.tl.tracks}
        self.assertEqual(list(tracks), list(otio_timeline.TRACKS))
        clips = [c for c in tracks["V1 画面"] if isinstance(c, otio.schema.Clip)]
        self.assertEqual(len([c for c in clips if not c.metadata["dramio"].get("freeze_last_frame")]), 13)
        self.assertEqual(len([c for c in tracks["A1 对白"] if isinstance(c, otio.schema.Clip)]), 15)
        self.assertEqual(len(tracks["A3 BGM"]) + len(tracks["A4 SFX"]), 0)
        self.assertEqual(int(round(self.tl.duration().value)), self.plan.total_frames)

    def test_placeholder_flags_and_freeze(self):
        v = {c.metadata["dramio"].get("shot_id"): c for c in self.tl.tracks[0] if not c.metadata["dramio"].get("freeze_last_frame")}
        self.assertFalse(v["ep01_sc01_sh05"].metadata["dramio"]["placeholder"])
        self.assertTrue(v["ep01_sc01_sh02"].metadata["dramio"]["placeholder"])
        freezes = [c for c in self.tl.tracks[0] if c.metadata["dramio"].get("freeze_last_frame")]
        self.assertEqual(len(freezes), 1)  # sc01_sh05：5.17 秒片段延长到 7.04 秒
        self.assertEqual(freezes[0].metadata["dramio"]["shot_id"], "ep01_sc01_sh05")

    def test_roundtrip_and_spec_is_plain_python(self):
        path = self.dir / "t.otio"
        otio_timeline.write(self.tl, path)
        text = otio.adapters.write_to_string(otio_timeline.read(path), adapter_name="otio_json")
        self.assertEqual(otio.adapters.write_to_string(otio.adapters.read_from_string(text, adapter_name="otio_json"), adapter_name="otio_json"), text)
        spec = otio_timeline.to_spec(otio_timeline.read(path), self.dir)
        self.assertEqual(len(spec["shots"]), 13)
        self.assertEqual(spec["total_frames"], self.plan.total_frames)
        self.assertIsInstance(spec["size"], list)
        json.dumps(spec)  # 全是普通类型
        self.assertGreater(next(s for s in spec["shots"] if s["shot_id"] == "ep01_sc01_sh05")["freeze_frames"], 0)

    def test_exchange_view_exports_and_reads_back(self):
        names = otio.adapters.available_adapter_names()
        view = otio_timeline.exchange_view(self.tl)
        self.assertEqual([t.name for t in view.tracks], ["V1 画面", "A1 对白", "A2 环境声"])
        for adapter in ("fcp_xml", "cmx_3600"):
            if adapter not in names:
                self.skipTest(f"没有 {adapter} 适配器（OpenTimelineIO-Plugins）")
            out = self.dir / f"x.{adapter}"
            otio_timeline.export(view, out, adapter)
            back = otio.adapters.read_from_file(str(out), adapter_name=adapter)
            self.assertEqual(int(round(back.duration().value)), self.plan.total_frames)

    def test_missing_dependency_gives_install_hint(self):
        import builtins
        real = builtins.__import__

        def no_otio(name, *a, **k):
            if name.startswith("opentimelineio"):
                raise ImportError(name)
            return real(name, *a, **k)

        builtins.__import__ = no_otio
        try:
            with self.assertRaises(otio_timeline.OtioMissing) as ctx:
                otio_timeline.require_otio()
        finally:
            builtins.__import__ = real
        self.assertIn("pip install", str(ctx.exception))


def tone(path: Path, seconds: float = 1.0) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-ac", "1", str(path)], check=True)
    return path


def still(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=144x256:rate=1:duration=1", "-frames:v", "1", str(path)], check=True)
    return path


@unittest.skipUnless(media.available() and HAS_FONT, "没有 ffmpeg / ffprobe 或中文字体")
class RenderTest(unittest.TestCase):
    """合成小素材（144×256）的渲染与增量缓存。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.clip = make_video(self.dir / "clip.mp4", seconds=2, size="144x256")
        self.png = still(self.dir / "key.png")
        self.wav = tone(self.dir / "l.wav")
        self.ass = self.dir / "s.ass"
        self.ass.write_text(subtitles.ass([subtitles.Cue("l_1", "s1", "dialogue", "你好", 0.2, 1.0)]), encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def spec(self, second_real: bool, ambient="off"):
        def shot(sid, start, real):
            return {"shot_id": sid, "source": "real" if real else "placeholder", "placeholder": not real, "media": str(self.clip if real else self.png),
                    "sha256": ("c" if real else "k") + sid, "start_frames": start, "native_frames": 48 if real else 36, "target_frames": 48,
                    "freeze_frames": 0, "zoom": None if real else {"from": 1.0, "to": 1.08}, "hint_s": 2.0}
        return {
            "fps": 24, "size": [144, 256], "ambient": ambient, "total_frames": 96, "shots": [shot("s1", 0, True), shot("s2", 48, second_real)],
            "lines": [{"sha256": "w1", "media": str(self.wav), "in_s": 0.0, "dur_s": 0.8, "start_frames": 4}],
            "ambient_clips": [{"sha256": "c" + "s1", "media": str(self.clip), "start_frames": 0, "dur_s": 2.0, "gain_db": -24.0, "enabled": ambient != "off"}],
            "cues": [], "badge": {"text": "AI生成"},
        }

    def run_render(self, spec, ambient="off"):
        return render.render(spec, self.dir / "out", self.dir / "cache", self.ass, ambient)

    def test_render_spec_and_metadata(self):
        r = self.run_render(self.spec(False))
        info = media.probe(r.final)
        self.assertEqual((info.width, info.height, info.fps), (144, 256, 24.0))
        self.assertEqual(info.nb_frames, 96)
        self.assertTrue(info.has_audio)
        self.assertIn("aigc", media.format_tags(r.final))
        self.assertEqual(json.loads(media.format_tags(r.final)["aigc"])["Label"], "1")
        audio = media.probe_audio(r.final)
        self.assertLess(abs(audio.duration_s - info.duration_s), 0.05)

    def test_second_run_is_all_hits(self):
        self.run_render(self.spec(False))
        again = self.run_render(self.spec(False))
        self.assertEqual(again.as_dict()["misses"], 0)

    def test_replacing_a_placeholder_only_recomputes_that_shot_and_final(self):
        self.run_render(self.spec(False))
        r = self.run_render(self.spec(True))
        missed = sorted((n.kind, n.name) for n in r.nodes if not n.hit)
        self.assertEqual(missed, [("final", "final"), ("segment", "s2")])

    def test_ambient_change_recomputes_audio_and_final_only(self):
        self.run_render(self.spec(False))
        r = self.run_render(self.spec(False, "low"), "low")
        self.assertEqual(sorted(n.kind for n in r.nodes if not n.hit), ["audio_mix", "final"])

    def test_audio_is_normalized(self):
        r = self.run_render(self.spec(False))
        loud = media.loudness(r.final)
        self.assertLess(abs(loud.integrated_lufs - render.LUFS_TARGET), 1.5)


class NoSwitchTest(unittest.TestCase):
    def test_aigc_label_cannot_be_turned_off(self):
        parser = argparse.ArgumentParser()
        compose_cmd.add_parser(parser.add_subparsers())
        flags = " ".join(a for action in parser._subparsers._group_actions[0].choices["compose"]._actions for a in action.option_strings)
        for word in ("badge", "aigc", "label", "watermark", "no-"):
            self.assertNotIn(word, flags.lower())
        for fn in (render.render, render.render_final, otio_timeline.build):
            self.assertFalse([n for n in inspect.signature(fn).parameters if any(w in n.lower() for w in ("badge", "aigc", "label", "watermark"))])


if __name__ == "__main__":
    unittest.main()
