import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from poc import __main__ as cli
from poc import compose, media, music, pricing, providers, script, tts, volc_sign

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
AK_SENT, SK_SENT = "AKLT-SENTINEL-music-ak-3f9", "sk-SENTINEL-music-5d2/x=="
ENV = {"VOLC_ACCESSKEY": AK_SENT, "VOLC_SECRETKEY": SK_SENT}
CJK = re.compile(r"[一-鿿]")


def episode():
    doc, _ = tts.load_episode(script.SAMPLE_EP01)
    return doc


def hint_plan():
    doc = episode()
    return doc, music.plan_bgm(doc, music.spans_from_hints(doc))


class MoodTableTest(unittest.TestCase):
    def test_table_matches_schema_enum_exactly(self):
        self.assertEqual(sorted(music.MOOD_TABLE), sorted(music.schema_enum("$defs", "scene", "properties", "mood")))

    def test_time_and_int_ext_maps_cover_schema(self):
        self.assertEqual(sorted(music.TIME_OF_DAY_CN), sorted(music.schema_enum("$defs", "setting", "properties", "time_of_day")))
        self.assertEqual(sorted(music.INT_EXT_CN), sorted(music.schema_enum("$defs", "setting", "properties", "int_ext")))

    def test_every_mood_renders_a_chinese_prompt_without_placeholders(self):
        doc = episode()
        scene = dict(doc["episodes"][0]["scenes"][0])
        for mood in music.MOOD_TABLE:
            scene["mood"] = mood
            text = music.render_prompt(doc, scene)
            self.assertNotIn("$", text, mood)
            self.assertNotRegex(text, r"[A-Za-z]{4,}", mood)  # 接口只支持中文描述（BPM 这样的缩写允许）
            self.assertGreater(len(CJK.findall(text)), 60, mood)
            self.assertLessEqual(len(text), 400, mood)
            self.assertIn("无人声", text)
            self.assertIn(music.MOOD_TABLE[mood]["emotion"], text)

    def test_unknown_mood_is_an_error(self):
        doc = episode()
        scene = dict(doc["episodes"][0]["scenes"][0], mood="furious")
        with self.assertRaises(music.MusicError):
            music.render_prompt(doc, scene)

    def test_prompt_does_not_leak_story_summary_or_names(self):
        doc = episode()
        scene = doc["episodes"][0]["scenes"][0]
        text = music.render_prompt(doc, scene)
        for name in (c["name"] for c in doc["characters"]):
            self.assertNotIn(name, text)
        self.assertNotIn(scene["summary"], text)


class PlanTest(unittest.TestCase):
    def test_hint_basis_covers_the_whole_episode_without_holes(self):
        doc, segs = hint_plan()
        self.assertEqual([s.scene_id for s in segs], [s["scene_id"] for s in doc["episodes"][0]["scenes"]])
        self.assertEqual([s.mood for s in segs], ["sad", "suspense", "tense"])
        self.assertEqual(music.check_coverage(segs, 60.0), [])
        self.assertEqual((segs[0].start_s, segs[-1].end_s), (0.0, 60.0))

    def test_min_generation_length_and_trim(self):
        _, segs = hint_plan()
        for s in segs:
            self.assertGreaterEqual(s.gen_s, music.MIN_GEN_S)
            self.assertAlmostEqual(s.trim_s, s.gen_s - s.play_s, places=3)
            self.assertGreaterEqual(s.trim_s, 0.0)

    def test_crossfades_overlap_neighbours_by_xfade(self):
        _, segs = hint_plan()
        for a, b in zip(segs, segs[1:]):
            self.assertAlmostEqual(a.end_s - b.start_s, music.XFADE_S, places=3)
            self.assertEqual((b.fade_in_s, a.fade_out_s), (music.XFADE_S, music.XFADE_S))
        self.assertEqual((segs[0].fade_in_s, segs[-1].fade_out_s), (0.0, music.TAIL_FADE_S))

    def test_estimate_is_gen_seconds_times_unit_price(self):
        _, segs = hint_plan()
        self.assertAlmostEqual(sum(s.est_cny for s in segs), sum(s.gen_s for s in segs) * 0.002, places=6)
        self.assertFalse(pricing.MUSIC_PRICE_VERIFIED)

    def test_node_key_is_stable_and_sensitive(self):
        _, segs = hint_plan()
        again = music.plan_bgm(episode(), music.spans_from_hints(episode()))
        self.assertEqual([s.node_key for s in segs], [s.node_key for s in again])
        self.assertEqual(len({s.node_key for s in segs}), 3)
        self.assertNotEqual(music.node_key("a", 30), music.node_key("a", 31))
        self.assertNotEqual(music.node_key("a", 30), music.node_key("b", 30))
        self.assertNotEqual(music.node_key("a", 30), music.node_key("a", 30, version="bgm.v2"))

    def test_scene_longer_than_the_limit_is_rejected(self):
        doc = episode()
        spans = [music.Span("ep01_sc01", 0.0, 130.0)] + music.spans_from_hints(doc)[1:]
        with self.assertRaises(music.MusicError):
            music.plan_bgm(doc, spans)

    def test_coverage_check_reports_holes(self):
        _, segs = hint_plan()
        segs[1].start_s += 5
        self.assertTrue(any("空洞" in p for p in music.check_coverage(segs, 60.0)))
        self.assertTrue(music.check_coverage([], 60.0))

    @unittest.skipUnless(HAVE_FFMPEG, "需要 ffprobe")
    def test_compose_basis_covers_the_final_cut(self):
        plan = music.build_plan(script.SAMPLE_EP01, "compose")
        self.assertAlmostEqual(plan["total_s"], 63.5, places=2)
        self.assertEqual(plan["coverage_problems"], [])
        self.assertAlmostEqual(plan["estimate"]["cost_cny"], 0.18, places=3)


class SfxTest(unittest.TestCase):
    EXPECTED = {
        "大雨声": "ambience", "旋转门转动声": "foley", "脚步踩水声": "foley_loop", "雨打伞面声": "ambience", "雨声渐大": "ambience",
        "窗外雨声": "ambience", "胶带撕开声": "foley", "键盘敲击声": "foley", "电脑提示音": "ui", "推门声": "foley",
        "议论声戛然而止": "crowd_cut", "手机震动声": "ui",
    }

    def test_every_ep01_tag_is_planned_once_with_its_category(self):
        doc = episode()
        tags = [t for sc in doc["episodes"][0]["scenes"] for sh in sc["shots"] for t in sh.get("sfx", [])]
        self.assertEqual(len(tags), 12)
        items = music.sfx_plan(doc, music.shot_spans_from_hints(doc))
        self.assertEqual(sorted(i.tag for i in items), sorted(tags))
        for i in items:
            self.assertEqual(i.category, self.EXPECTED[i.tag], i.tag)
            self.assertEqual(i.status, "unsourced")
            self.assertEqual(len(i.sources), 3)

    def test_unknown_tag_is_listed_not_dropped(self):
        doc = episode()
        doc["episodes"][0]["scenes"][0]["shots"][3]["sfx"] = ["远处的汽笛"]
        items = music.sfx_plan(doc, music.shot_spans_from_hints(doc))
        odd = [i for i in items if i.tag == "远处的汽笛"]
        self.assertEqual([i.category for i in odd], ["uncategorized"])
        self.assertIn("未归类 1 条：远处的汽笛", music.render_text(music.build_plan(script.SAMPLE_EP01, "hint") | {"sfx": [asdict(i) | {"sources": []} for i in items]}))

    def test_ambience_spans_the_shot_and_point_effects_stay_inside_it(self):
        doc = episode()
        spans = music.shot_spans_from_hints(doc)
        for i in music.sfx_plan(doc, spans):
            start, end = spans[i.shot_id]
            self.assertGreaterEqual(i.start_s, start - 1e-9)
            self.assertLessEqual(i.end_s, end + 1e-9)
            if i.category in ("ambience", "foley_loop"):
                self.assertEqual((i.start_s, i.end_s), (start, end))
                self.assertTrue(i.loop)

    def test_rule_order_prefers_footsteps_over_water(self):
        self.assertEqual(music.classify_sfx("脚步踩水声").category, "foley_loop")
        self.assertEqual(music.classify_sfx("雨打伞面声").category, "ambience")


class CliTest(unittest.TestCase):
    def run_cli(self, *argv, env=None):
        out = io.StringIO()
        args = cli.build_parser().parse_args(["music", *argv])
        return args.func(args, out=out, env={} if env is None else env), out.getvalue()

    def test_dry_run_needs_no_keys_and_writes_no_runs(self):
        before = set(p.name for p in (Path(music.config.PROJECT_DIR) / "runs").glob("*")) if (Path(music.config.PROJECT_DIR) / "runs").exists() else set()
        code, text = self.run_cli("--dry-run", "--basis", "hint")
        self.assertEqual(code, 0)
        after = set(p.name for p in (Path(music.config.PROJECT_DIR) / "runs").glob("*")) if (Path(music.config.PROJECT_DIR) / "runs").exists() else set()
        self.assertEqual(before, after)
        self.assertIn("ep01_sc01", text)
        self.assertIn("bgm.v1", text)
        self.assertIn("≈ ¥0.180", text)
        self.assertIn("未核对账单", text)
        self.assertIn("覆盖检查：通过", text)

    def test_dry_run_json_is_parseable(self):
        code, text = self.run_cli("--dry-run", "--basis", "hint", "--json")
        plan = json.loads(text)
        self.assertEqual((code, plan["basis"], len(plan["segments"]), len(plan["sfx"])), (0, "hint", 3, 12))

    def test_missing_keys_without_dry_run_exits_2_with_hint(self):
        code, text = self.run_cli("--basis", "hint", env={"VOLC_ACCESSKEY": "only-ak"})
        self.assertEqual(code, 2)
        self.assertIn("VOLC_SECRETKEY", text)
        self.assertIn("--dry-run", text)
        self.assertNotIn("only-ak", text)

    def test_modes_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--dry-run", "--analyze", "x")

    def test_export_writes_plan_json(self):
        with tempfile.TemporaryDirectory() as d:
            code, _ = self.run_cli("--export", d, "--basis", "hint")
            self.assertEqual(code, 0)
            plan = json.loads((Path(d) / "music-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(len(plan["segments"]), 3)

    def test_analyze_missing_file_fails_with_reason(self):
        code, text = self.run_cli("--analyze", "/nonexistent/x.wav", "--basis", "hint")
        self.assertEqual(code, 1)
        self.assertIn("文件不存在", text)


class RegistryTest(unittest.TestCase):
    def test_provider_registered_and_doctor_sees_it(self):
        p = providers.BY_NAME["volc_music"]
        self.assertEqual((p.env, p.capabilities, p.domains, p.probe), (("VOLC_ACCESSKEY", "VOLC_SECRETKEY"), ("music_sfx",), ("open.volcengineapi.com",), None))
        self.assertIn("VOLC_ACCESSKEY", providers.all_env_vars())

    def test_music_price(self):
        self.assertAlmostEqual(pricing.music_cny("volc_music", "GenBGMForTime", 200), 0.4)  # 文档示例：200 秒 = ¥0.4


def tone(path: Path, seconds: float, volume_db: float = 0.0, fade_out: float | None = None, silence_tail: float = 0.0) -> Path:
    af = f"volume={volume_db}dB"
    if fade_out:
        af += f",afade=t=out:st={seconds - fade_out}:d={fade_out}"
    if silence_tail:
        af += f",apad=pad_dur={silence_tail}"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000", "-af", af, str(path)],
        check=True,
    )
    return path


@unittest.skipUnless(HAVE_FFMPEG, "需要 ffmpeg")
class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_level_difference_is_measured_to_within_tolerance(self):
        loud = music.analyze_file(tone(self.dir / "a.wav", 5.0, -12.0))
        quiet = music.analyze_file(tone(self.dir / "b.wav", 5.0, -18.0))
        self.assertAlmostEqual(loud.lufs - quiet.lufs, 6.0, delta=0.3)
        self.assertAlmostEqual(loud.duration_s, 5.0, delta=0.05)
        self.assertEqual((loud.codec, loud.sample_rate), ("pcm_s16le", 48000))
        self.assertIsNotNone(loud.true_peak_dbtp)
        self.assertIsNotNone(loud.lra_lu)

    def test_expected_duration_delta(self):
        rep = music.analyze_file(tone(self.dir / "c.wav", 5.0), expected_s=6.0)
        self.assertAlmostEqual(rep.duration_delta_s, -1.0, delta=0.05)

    def test_trailing_silence_and_natural_ending(self):
        padded = music.analyze_file(tone(self.dir / "d.wav", 5.0, silence_tail=1.0))
        self.assertAlmostEqual(padded.trailing_silence_s, 1.0, delta=0.15)
        self.assertEqual(padded.ending, "natural")
        faded = music.analyze_file(tone(self.dir / "e.wav", 6.0, fade_out=2.0))
        self.assertLessEqual(faded.tail_drop_db, -6.0)
        self.assertEqual(faded.ending, "natural")
        abrupt = music.analyze_file(tone(self.dir / "f.wav", 6.0))
        self.assertEqual(abrupt.ending, "needs_fade")
        self.assertLess(abs(abrupt.tail_drop_db), 1.0)
        self.assertEqual(abrupt.trailing_silence_s, 0.0)

    def test_leading_silence(self):
        src = tone(self.dir / "g0.wav", 4.0)
        out = self.dir / "g.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-af", "adelay=1500|1500", str(out)], check=True)
        self.assertAlmostEqual(music.analyze_file(out).leading_silence_s, 1.5, delta=0.15)

    def test_suggested_gain_targets_below_the_reference(self):
        rep = music.analyze_file(tone(self.dir / "h.wav", 5.0, -12.0), reference_lufs=-16.0, below_db=18.0)
        self.assertAlmostEqual(rep.lufs + rep.suggested_gain_db, -34.0, delta=0.15)
        self.assertIn("待在线样本调参", rep.suggestion_note)

    def test_not_audio_and_missing_file_fail(self):
        junk = self.dir / "junk.wav"
        junk.write_bytes(b"not audio")
        with self.assertRaises(media.MediaError):
            music.analyze_file(junk)
        with self.assertRaises(media.MediaError):
            music.analyze_paths([self.dir / "nope.wav"], [], None)

    def test_directory_matches_files_to_scenes_by_stem(self):
        _, segs = hint_plan()
        tone(self.dir / "ep01_sc01.wav", 3.0)
        reports = music.analyze_paths([self.dir], segs, None)
        self.assertEqual(reports[0].expected_s, 30.0)
        self.assertIn("ep01_sc01.wav", music.render_report(reports))


class FakeNet:
    """按动作分流的假 transport：提交 / 查询 / 下载。"""

    def __init__(self, fail_submit=(), statuses=(2,), audio=b"", status_code=200, fail_query=(), fail_download=()):
        self.fail_submit = list(fail_submit)
        self.fail_query = list(fail_query)
        self.fail_download = list(fail_download)
        self.statuses = list(statuses)
        self.requests = []
        self.audio = audio

    def __call__(self, request, timeout):
        self.requests.append(request)
        url = request.full_url
        if "Action=GenBGMForTime" in url:
            if self.fail_submit:
                code = self.fail_submit.pop(0)
                return volc_sign.Response(200, {}, json.dumps({"Code": code, "Message": "busy"}).encode())
            n = len([r for r in self.requests if "GenBGMForTime" in r.full_url])
            return volc_sign.Response(200, {}, json.dumps({"Code": 0, "Result": {"TaskID": f"task-{n}", "PredictedWaitTime": 1}}).encode())
        if "Action=QuerySong" in url:
            if self.fail_query:
                code = self.fail_query.pop(0)
                return volc_sign.Response(200, {}, json.dumps({"Code": code, "Message": "busy"}).encode())
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            detail = {"AudioUrl": "https://v1-default.douyinvod.com/a.wav?sig=1", "Duration": 30.5} if st == 2 else {}
            return volc_sign.Response(200, {}, json.dumps({"Code": 0, "Result": {"TaskID": "t", "Status": st, "SongDetail": detail}}).encode())
        if self.fail_download:
            return volc_sign.Response(self.fail_download.pop(0), {}, b"")
        return volc_sign.Response(200, {}, self.audio)

    def count(self, action):
        return len([r for r in self.requests if f"Action={action}" in r.full_url])


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.segs = hint_plan()[1]

    def gen(self, net, **kw):
        out = io.StringIO()
        kw.setdefault("max_cost_cny", 5.0)
        kw.setdefault("probe", lambda p: media.AudioInfo("pcm_s16le", 48000, 2, 30.5))
        res = music.generate(
            self.segs, self.dir / "out", ENV, base_dir=self.dir / "runs", transport=net, sleep=lambda s: None, out=out, **kw,
        )
        return res, out.getvalue()

    def test_success_writes_files_manifest_and_costs(self):
        net = FakeNet(audio=b"RIFFfake")
        res, text = self.gen(net)
        self.assertTrue(all(r.ok for r in res))
        self.assertEqual([r.file for r in res], ["ep01_sc01.wav", "ep01_sc02.wav", "ep01_sc03.wav"])
        manifest = json.loads((self.dir / "out" / "bgm-manifest.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(manifest["cost_cny"], 3 * 30.5 * 0.002, places=5)  # 按返回的 Duration 计费
        self.assertEqual(manifest["results"][0]["sha256"], __import__("hashlib").sha256(b"RIFFfake").hexdigest())
        self.assertFalse(list((self.dir / "out").glob(".*.part")))

    def test_credentials_never_reach_any_output_file(self):
        self.gen(FakeNet(audio=b"x"))
        for path in self.dir.rglob("*"):
            if path.is_file():
                data = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn(AK_SENT, data, path.name)
                self.assertNotIn(SK_SENT, data, path.name)
        for r in FakeNet(audio=b"x").requests:
            pass

    def test_calls_are_recorded_with_node_key(self):
        self.gen(FakeNet(audio=b"x"), only="ep01_sc01")
        runs = list((self.dir / "runs").glob("*"))
        self.assertEqual(len(runs), 1)
        calls = [json.loads(line) for line in (runs[0] / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual({c["node_key"] for c in calls}, {self.segs[0].node_key})
        self.assertEqual([c["capability"] for c in calls], ["music_sfx", "music_sfx"])
        self.assertGreater(calls[1]["cost_cny"], 0)
        self.assertEqual(calls[0]["cost_cny"], 0.0)

    def test_transient_submit_error_is_retried(self):
        net = FakeNet(fail_submit=[200023], audio=b"x")
        res, _ = self.gen(net, only="ep01_sc01")
        self.assertEqual((res[0].ok, res[0].attempts), (True, 2))

    def test_permanent_error_stops_after_one_attempt_and_is_not_charged(self):
        net = FakeNet(fail_submit=[200022], audio=b"x")
        res, _ = self.gen(net, only="ep01_sc01")
        self.assertEqual((res[0].ok, res[0].attempts, res[0].cost_cny), (False, 1, 0.0))
        self.assertIn("200022", res[0].error)

    def test_transient_poll_error_retries_the_same_task_without_resubmitting(self):
        net = FakeNet(fail_query=[200023], audio=b"x")
        res, _ = self.gen(net, only="ep01_sc01")
        self.assertTrue(res[0].ok)
        self.assertEqual((net.count("GenBGMForTime"), net.count("QuerySong")), (1, 2))

    def test_transient_download_error_retries_the_download_only(self):
        net = FakeNet(fail_download=[502], audio=b"x")
        res, _ = self.gen(net, only="ep01_sc01")
        self.assertTrue(res[0].ok)
        self.assertEqual(net.count("GenBGMForTime"), 1)

    def test_unresolved_submitted_task_counts_toward_the_cost_guard(self):
        net = FakeNet(fail_download=[502, 502, 502, 502], audio=b"x")
        res, text = self.gen(net, only="ep01_sc01")
        self.assertFalse(res[0].ok)
        self.assertEqual(net.count("GenBGMForTime"), 1)  # 没有为取不回结果而重新提交
        self.assertAlmostEqual(res[0].cost_cny, self.segs[0].est_cny)
        self.assertIn("可能已计费", res[0].error)
        manifest = json.loads((self.dir / "out" / "bgm-manifest.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(manifest["cost_cny"], self.segs[0].est_cny)

    def test_retryable_task_failure_resubmits_and_is_not_charged(self):
        net = FakeNet(statuses=[3, 2], audio=b"x")
        res, _ = self.gen(net, only="ep01_sc01")
        self.assertEqual(res[0].ok, False)  # 失败码 300061 不可重试
        # 300067 才可重试
        resp = lambda st: volc_sign.Response(200, {}, json.dumps({"Code": 0, "Result": {"TaskID": "t", "Status": st, "FailureReason": {"Code": 300067, "Msg": "retry"}, "SongDetail": {"AudioUrl": "https://v1-default.douyinvod.com/a.wav", "Duration": 30.0} if st == 2 else {}}}).encode())
        class Net2(FakeNet):
            def __call__(self, request, timeout):
                if "Action=QuerySong" in request.full_url:
                    self.requests.append(request)
                    return resp(3 if self.count("QuerySong") == 1 else 2)
                return super().__call__(request, timeout)
        net2 = Net2(audio=b"x")
        res, _ = self.gen(net2, only="ep01_sc01")
        self.assertTrue(res[0].ok)
        self.assertEqual((net2.count("GenBGMForTime"), res[0].attempts), (2, 2))
        self.assertAlmostEqual(res[0].cost_cny, 30.0 * 0.002)

    def test_probe_failure_after_billing_is_reported_without_resubmitting(self):
        def bad_probe(path):
            raise media.MediaError("ffprobe 失败")
        net = FakeNet(audio=b"x")
        res, _ = self.gen(net, only="ep01_sc01", probe=bad_probe)
        self.assertFalse(res[0].ok)
        self.assertEqual(net.count("GenBGMForTime"), 1)
        self.assertGreater(res[0].cost_cny, 0)  # 已计费，账不能丢
        self.assertIn("ffprobe", res[0].error)

    def test_failed_task_is_reported(self):
        res, _ = self.gen(FakeNet(statuses=[3]), only="ep01_sc01")
        self.assertFalse(res[0].ok)
        self.assertIn("task_failed", res[0].error)

    def test_cost_limit_stops_before_submitting(self):
        net = FakeNet(audio=b"x")
        with self.assertRaises(music.CostLimit):
            self.gen(net, max_cost_cny=0.05)
        self.assertFalse([r for r in net.requests if "GenBGMForTime" in r.full_url])

    def test_unknown_scene_and_missing_keys(self):
        with self.assertRaises(music.MusicError):
            self.gen(FakeNet(), only="nope")
        with self.assertRaises(volc_sign.VolcError):
            music.generate(self.segs, self.dir / "o2", {"VOLC_ACCESSKEY": "a"}, max_cost_cny=1, base_dir=self.dir / "r2")


if __name__ == "__main__":
    unittest.main()
