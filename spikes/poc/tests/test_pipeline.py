import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from poc import llm, nodecache, nodekey, otio_timeline, pipeline, script, seedream, shots, speech, tts, video
from tests.test_media import make_video
from tests.test_seedream import jpeg
from tests.test_tts import fake_mp3

try:
    import opentimelineio  # noqa: F401
except ImportError:
    opentimelineio = None

LOGLINE = "雨夜，被陷害的设计师当众撕开真相。"
ENV = {"DEEPSEEK_API_KEY": "sk-SENTINEL-1", "ARK_API_KEY": "ark-SENTINEL-2", "VOLC_SPEECH_API_KEY": "vs-SENTINEL-3"}


def base_doc():
    doc = json.loads(script.SAMPLE_EP01.read_text(encoding="utf-8"))
    doc["series"]["logline"] = LOGLINE
    return doc


class FakeWorld:
    """所有供应商的伪造实现，记录调用；edit(doc) 可以改动“LLM 输出”的剧本。"""

    def __init__(self, edit=None, video_outcomes=None):
        self.edit = edit
        self.chat_calls, self.gen_calls, self.tts_calls, self.video_calls = [], [], [], []
        self.video_outcomes = video_outcomes or {}  # shot_id → "ok" / "quota"
        self.jobs = {}

    def doc(self):
        doc = base_doc()
        if self.edit:
            self.edit(doc)
        return doc

    def chat(self, provider, model, messages, **kw):
        is_script = messages[0]["content"] == script.system_prompt(60, "script.v1")
        self.chat_calls.append("script" if is_script else "shots")
        doc = self.doc()
        if is_script:
            text = json.dumps(doc, ensure_ascii=False)
        else:
            out = {"scenes": [{"scene_id": sc["scene_id"], "shots": sc["shots"]} for sc in doc["episodes"][0]["scenes"]]}
            text = json.dumps(out, ensure_ascii=False)
        usage = {"prompt_tokens": 1000, "completion_tokens": 2000, "total_tokens": 3000}
        return llm.ChatResult(text=text, finish_reason="stop", usage=usage, request_id="req", model=model)

    def gen(self, prompt, *, model, size, refs, watermark, output_format, env):
        self.gen_calls.append(prompt)
        w, h = (int(x) for x in size.split("x"))
        img = seedream.GeneratedImage(jpeg(w, h, payload=prompt.encode()[:64]), None, size, "jpeg")
        return seedream.ImageResult([img], {"generated_images": 1}, "rid", model)

    def tts_fn(self, text, voice, *, resource, speech_rate, context_text, env):
        self.tts_calls.append(text)
        return speech.TTSResult(fake_mp3(text), len(text), 1, "log")

    def submit(self, mp3, *, env):
        rid = f"rid-{len(self.jobs)}"
        self.jobs[rid] = mp3
        return rid

    def query(self, rid, *, env):
        seconds = tts.audio.mp3_info(self.jobs[rid]).duration_s
        return 20000000, {"audio_info": {"duration": round(seconds * 1000)},
                          "result": {"text": "识别", "utterances": [{"text": "识别", "start_time": 100, "end_time": round(seconds * 1000) - 100}]}}

    def video_fn(self, job, out_dir):
        self.video_calls.append(job.shot_id)
        outcome = self.video_outcomes.get(job.shot_id, "ok")
        if outcome == "quota":
            return {"jobs": [{"shot_id": job.shot_id, "status": "error", "error": "rate_limit: 429 2056 已达到 Token Plan 用量上限"},
                             {"status": "aborted", "error": "额度用尽"}], "count": 1, "success": 0}
        (out_dir / "videos").mkdir(parents=True, exist_ok=True)
        make_video(out_dir / "videos" / f"{job.shot_id}.mp4", seconds=job.duration, size="72x128")
        return {"jobs": [{"shot_id": job.shot_id, "status": "succeeded"}], "count": 1, "success": 1}

    def render(self, spec, out_dir, cache_dir, ass_path, ambient):
        from poc import render
        out_dir.mkdir(parents=True, exist_ok=True)
        make_video(out_dir / "final.mp4", seconds=1, size="108x192")
        res = render.Result()
        res.final = out_dir / "final.mp4"
        res.nodes.append(render.Node("final", "final", "k" * 64, False, str(res.final)))
        return res

    def fns(self):
        return pipeline.Fns(self.chat, self.gen, self.tts_fn, self.submit, self.query, self.video_fn, self.render, sleep=lambda s: None)


@unittest.skipUnless(opentimelineio, "没有安装 opentimelineio")
class PipelineTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def run_pipeline(self, world, **kw):
        opts = pipeline.Options(logline=LOGLINE, cache_dir=self.dir / "cache", base_dir=self.dir / "runs", video=kw.pop("video", "placeholder"), **kw)
        pl = pipeline.Pipeline(opts, world.fns(), env=ENV)
        return pl, pl.run_all()

    def misses(self, manifest):
        return sorted((n["stage"], n["id"]) for n in manifest["nodes"] if not n["hit"] and not n["stage"].startswith("compose"))

    def test_cold_then_hot_run(self):
        w = FakeWorld()
        pl, cold = self.run_pipeline(w)
        self.assertEqual(w.chat_calls, ["script", "shots"])
        self.assertEqual(len(w.gen_calls), 2 + 2 + 13)
        self.assertEqual(len(w.tts_calls), 15)
        self.assertEqual(w.video_calls, [])
        self.assertEqual(cold["hits"], 0)
        self.assertGreater(cold["cost_cny"], 0)
        self.assertEqual(len(cold["placeholders"]), 13)
        self.assertEqual(cold["degraded"], [])  # --video placeholder 是主动选择
        self.assertTrue((pl.run.dir / "final.mp4").is_file())
        self.assertTrue((pl.run.dir / "timeline.otio").is_file())
        # 热运行：同一缓存、同样输入 → 不调用任何供应商，费用为 0
        w2 = FakeWorld()
        pl2, hot = self.run_pipeline(w2)
        self.assertEqual((w2.chat_calls, w2.gen_calls, w2.tts_calls, w2.video_calls), ([], [], [], []))
        self.assertEqual(hot["misses"] - sum(1 for n in hot["nodes"] if n["stage"].startswith("compose") and not n["hit"]), 0)
        self.assertEqual(hot["cost_cny"], 0.0)
        self.assertEqual(pl2.run.calls_path.read_text(encoding="utf-8"), "")
        self.assertEqual(cold["ir_sha256"], hot["ir_sha256"])
        keys = lambda m: sorted((n["stage"], n["id"], n["key"]) for n in m["nodes"] if not n["stage"].startswith("compose"))  # noqa: E731
        self.assertEqual(keys(cold), keys(hot))

    def test_manifest_cost_equals_calls(self):
        w = FakeWorld()
        pl, man = self.run_pipeline(w)
        total = sum(float(json.loads(x).get("cost_cny") or 0) for x in pl.run.calls_path.read_text(encoding="utf-8").splitlines())
        self.assertAlmostEqual(total, man["cost_cny"], places=6)
        self.assertAlmostEqual(sum(n["cost_cny"] for n in man["nodes"]), man["cost_cny"], places=6)
        self.assertNotIn("SENTINEL", (pl.run.dir / "pipeline-manifest.json").read_text(encoding="utf-8"))

    def test_changing_one_line_only_redoes_that_line(self):
        self.run_pipeline(FakeWorld())

        def edit(doc):
            doc["episodes"][0]["scenes"][0]["shots"][1]["dialogue"][0]["text"] += "真的。"

        line_id = base_doc()["episodes"][0]["scenes"][0]["shots"][1]["dialogue"][0]["line_id"]
        w = FakeWorld(edit=edit)
        _, man = self.run_pipeline(w, reroll={"script": 1})
        self.assertEqual(self.misses(man), [("script", ""), ("shots", ""), ("tts", line_id)])
        self.assertEqual(len(w.tts_calls), 1)
        self.assertEqual(w.gen_calls, [])

    def test_changing_one_shot_only_redoes_that_keyframe(self):
        self.run_pipeline(FakeWorld())
        sid = base_doc()["episodes"][0]["scenes"][0]["shots"][0]["shot_id"]

        def edit(doc):
            shot = next(s for sc in doc["episodes"][0]["scenes"] for s in sc["shots"] if s["shot_id"] == sid)
            shot["description"] += "，镜头缓缓上移"
            if shot["characters"]:
                shot["characters"][0]["action"] += "，停顿"

        w = FakeWorld(edit=edit)
        _, man = self.run_pipeline(w, reroll={"script": 1})
        miss = self.misses(man)
        self.assertEqual({m[0] for m in miss}, {"script", "shots", "keyframe"})
        self.assertEqual([m for m in miss if m[0] == "keyframe"], [("keyframe", sid)])
        self.assertEqual(w.tts_calls, [])

    def test_quota_exhausted_degrades_and_stops_submitting(self):
        w = FakeWorld(video_outcomes={sid: "quota" for sid in ["ep01_sc01_sh01"]})
        pl, man = self.run_pipeline(w, video="auto")
        self.assertEqual(w.video_calls, ["ep01_sc01_sh01"])  # 第一个被拒后不再提交
        self.assertEqual(len(man["degraded"]), 13)
        self.assertIn("2056", man["degraded"][0]["reason"])
        self.assertEqual(len(man["placeholders"]), 13)
        self.assertFalse(man["strict_failed"])
        _, strict = self.run_pipeline(FakeWorld(video_outcomes={"ep01_sc01_sh01": "quota"}), video="auto", require_real_video=True)
        self.assertTrue(strict["strict_failed"])

    def test_video_exception_degrades_and_manifest_is_redacted_and_strict_verified(self):
        w = FakeWorld()

        def boom(job, out_dir):
            raise RuntimeError("provider said key=" + ENV["ARK_API_KEY"])

        w.video_fn = boom
        pl, man = self.run_pipeline(w, video="h3")
        self.assertEqual(len(man["degraded"]), len(man["placeholders"]))
        self.assertTrue(man["strict_failed"])
        text = (pl.run.dir / "pipeline-manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("SENTINEL", text)
        self.assertTrue(json.loads(text)["strict_failed"])

    def test_partial_video_is_cached_and_rest_resumes(self):
        order = [s["shot_id"] for sc in base_doc()["episodes"][0]["scenes"] for s in sc["shots"]]
        w = FakeWorld(video_outcomes={sid: "quota" for sid in order[3:4]})
        _, man = self.run_pipeline(w, video="auto")
        self.assertEqual(w.video_calls, order[:4])
        self.assertEqual(len(man["placeholders"]), 10)
        w2 = FakeWorld()  # 额度恢复：已成功的 3 个命中缓存，只补缺失的 10 个
        _, man2 = self.run_pipeline(w2, video="auto")
        self.assertEqual(w2.video_calls, order[3:])
        self.assertEqual(man2["placeholders"], [])
        self.assertEqual(man2["degraded"], [])

    def test_budget_guard(self):
        with self.assertRaises(pipeline.PipelineError) as cm:
            self.run_pipeline(FakeWorld(), max_cost_cny=0.5)
        self.assertIn("费用保险", str(cm.exception))

    def test_shots_failure_falls_back_to_script_shots(self):
        w = FakeWorld()
        real_chat = w.chat

        def chat(provider, model, messages, **kw):
            if messages[0]["content"] != script.system_prompt(60, "script.v1"):
                raise llm.LLMError("http", "boom", 400)
            return real_chat(provider, model, messages, **kw)

        w.chat = chat
        _, man = self.run_pipeline(w)
        self.assertEqual(man["degraded"][0]["stage"], "shots")
        self.assertEqual(len(man["placeholders"]), 13)


class NodeKeyTest(unittest.TestCase):
    base = dict(kind="keyframe", version="v1", params={"model": "m", "size": "1x1"}, template_sha256="a" * 64, ir={"prompt": "p"}, upstream=["b" * 64])

    def key(self, **over):
        args = copy.deepcopy(self.base) | over
        return nodekey.node_key(args["kind"], args["version"], args["params"], template_sha256=args["template_sha256"], ir=args["ir"], upstream=args["upstream"])

    def test_stable_and_order_independent(self):
        self.assertEqual(self.key(), self.key())
        self.assertEqual(self.key(params={"size": "1x1", "model": "m"}), self.key())

    def test_every_input_matters(self):
        k = self.key()
        for over in ({"kind": "tts"}, {"version": "v2"}, {"params": {"model": "n", "size": "1x1"}}, {"template_sha256": "c" * 64},
                     {"ir": {"prompt": "q"}}, {"upstream": ["d" * 64]}, {"upstream": ["b" * 64, "d" * 64]}):
            self.assertNotEqual(self.key(**over), k, over)

    def test_no_paths_or_time_and_upstream_must_be_sha(self):
        with self.assertRaises(ValueError):
            self.key(upstream=["/tmp/x.png"])
        self.assertEqual(len(self.key()), 64)

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            self.key(params={"x": float("nan")})


class CacheTest(unittest.TestCase):
    def test_roundtrip_hit_miss_and_corruption(self):
        with tempfile.TemporaryDirectory() as td:
            c = nodecache.NodeCache(Path(td))
            key = nodekey.sha256_json("k")
            self.assertIsNone(c.get("tts", key))
            rec = c.put("tts", key, {"mp3": b"abc", "asr": b"{}"}, cost_cny=0.01)
            hit = c.get("tts", key)
            self.assertEqual(c.read(hit, "mp3"), b"abc")
            self.assertEqual(hit.cost_cny, 0.01)
            c.cas_path(rec.artifacts["mp3"]["sha256"]).write_bytes(b"tampered")
            self.assertIsNone(c.get("tts", key))  # 产物损坏 → 未命中
            self.assertIsNone(c.get("video", key))  # 类型不同 → 未命中
            c.put("tts", key, {"mp3": b"abc", "asr": b"{}"})  # 自愈：重新 put 覆盖损坏的 blob
            self.assertEqual(c.read(c.get("tts", key), "mp3"), b"abc")

    def test_malformed_record_is_a_miss(self):
        with tempfile.TemporaryDirectory() as td:
            c = nodecache.NodeCache(Path(td))
            key = nodekey.sha256_json("k")
            path = c._node_path("tts", key)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"kind": "tts", "key": key, "artifacts": {"mp3": "oops"}}), encoding="utf-8")
            self.assertIsNone(c.get("tts", key))

    def test_rejects_bad_kind_or_key(self):
        c = nodecache.NodeCache(Path("/nonexistent"))
        with self.assertRaises(ValueError):
            c.get("../x", "0" * 64)
        with self.assertRaises(ValueError):
            c.get("tts", "abc")


class VoiceTest(unittest.TestCase):
    def test_assign_by_gender(self):
        voices, warns = pipeline.assign_voices(base_doc())
        self.assertEqual(voices["char_suwan"], pipeline.VOICE_POOL["female"][0])
        self.assertEqual(voices["char_luchen"], pipeline.VOICE_POOL["male"][0])
        self.assertEqual(warns, [])

    def test_extra_same_gender_reuses_with_warning(self):
        doc = base_doc()
        doc["characters"].append({**doc["characters"][0], "id": "char_x"})
        doc["episodes"][0]["scenes"][0]["shots"][0]["dialogue"].append(copy.deepcopy(doc["episodes"][0]["scenes"][0]["shots"][1]["dialogue"][0]) | {"line_id": "l_9999", "speaker": "char_x"})
        voices, warns = pipeline.assign_voices(doc)
        self.assertEqual(voices["char_x"], voices["char_suwan"])
        self.assertEqual(len(warns), 1)


if __name__ == "__main__":
    unittest.main()
