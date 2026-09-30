import contextlib
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from poc import costume, images, keyframe, pricing, seedream
from tests.test_costume import ENV, SENTINEL, FakeGen
from tests.test_seedream import jpeg

SUWAN, LUCHEN = "char_suwan", "char_luchen"
EMPTY = "ep01_sc01_sh01"
SOLO = "ep01_sc01_sh02"  # 苏晚
PAIR_LU_FIRST = "ep01_sc01_sh03"  # 双人，shot.characters 里陆沉在前
PAIR = "ep01_sc01_sh05"
FAR = "ep01_sc02_sh01"
HAND = "ep01_sc02_sh02"


def make_ref_root(base: Path) -> Path:
    """合成的 P0-06 证据目录：main-pro 的 main-01 / main-02、derive-ref-pro 的 expression-neutral / angry（验证按 manifest 选定）。"""
    root = base / "p006"
    for dirname, kind, labels in (("main-pro", "main", ("01", "02")), ("derive-ref-pro", "expression", ("angry", "neutral"))):
        entries = []
        for cid in (SUWAN, LUCHEN):
            for label in labels:
                data = jpeg(512, 896, payload=f"{dirname}-{cid}-{label}".encode())
                sha = hashlib.sha256(data).hexdigest()
                rel = f"{cid}/{kind}-{label}-{sha[:8]}.jpg"
                (root / dirname / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / dirname / rel).write_bytes(data)
                entries.append({"char_id": cid, "kind": kind, "label": label, "file": rel, "sha256": sha, "committed": True})
        (root / dirname / "manifest.json").write_text(json.dumps({"images": entries}), encoding="utf-8")
    return root


def ref_for(root: Path, dirname: str, cid: str, label: str) -> str:
    man = json.loads((root / dirname / "manifest.json").read_text(encoding="utf-8"))
    return next(e["sha256"] for e in man["images"] if e["char_id"] == cid and e["label"] == label)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_ref_root(self.base)
        self.doc, _ = costume.load_episode()
        self.library = keyframe.load_library(self.root, [SUWAN, LUCHEN])

    def tearDown(self):
        self.tmp.cleanup()

    def jobs(self, schemes, shots=None):
        return keyframe.plan_jobs(self.doc, schemes, shots=shots, library=self.library)

    def one(self, scheme, shot):
        (job,) = [j for j in self.jobs([scheme], [shot])]
        return job


class ReferenceLibraryTest(Base):
    def test_selected_by_manifest_not_by_order(self):
        for cid in (SUWAN, LUCHEN):
            self.assertEqual(self.library[cid]["main"].sha256, ref_for(self.root, "main-pro", cid, "02"))
            self.assertEqual(self.library[cid]["neutral"].sha256, ref_for(self.root, "derive-ref-pro", cid, "neutral"))
        info = keyframe.library_info(self.library)
        self.assertEqual(set(info[SUWAN]), {"main", "neutral"})

    def test_repo_evidence_selects_d006_main_images(self):
        lib = keyframe.load_library(keyframe.DEFAULT_REF_ROOT, [SUWAN, LUCHEN])
        self.assertTrue(lib[SUWAN]["main"].sha256.startswith("4e6cf20a"))  # D-006：苏晚 pro main-02
        self.assertTrue(lib[LUCHEN]["main"].sha256.startswith("33b545ed"))  # D-006：陆沉 pro main-02
        self.assertIn("expression-neutral-", lib[SUWAN]["neutral"].path)

    def test_missing_manifest_entry_file_and_sha_mismatch(self):
        with self.assertRaisesRegex(keyframe.RefError, "缺少 P0-06 manifest"):
            keyframe.load_library(self.base / "nowhere", [SUWAN])
        with self.assertRaisesRegex(keyframe.RefError, "没有 char_x"):
            keyframe.load_library(self.root, ["char_x"])
        victim = next((self.root / "main-pro" / LUCHEN).glob("main-02-*.jpg"))
        victim.write_bytes(victim.read_bytes() + b"x")
        with self.assertRaisesRegex(keyframe.RefError, "sha256 与 manifest 不一致"):
            keyframe.load_library(self.root, [LUCHEN])
        victim.unlink()
        with self.assertRaisesRegex(keyframe.RefError, "参考图不可用"):
            keyframe.load_library(self.root, [LUCHEN])


class PromptTest(Base):
    def test_fields_come_from_ep01(self):
        shot = next(sh for _, sh in keyframe.shots_of(self.doc) if sh["shot_id"] == PAIR)
        scene = next(sc for sc in self.doc["episodes"][0]["scenes"] if sh_in(sc, PAIR))
        job = self.one("text", PAIR)
        want = [
            self.doc["series"]["visual_style"], scene["setting"]["location"], scene["setting"]["description"],
            shot["framing"]["composition"], shot["description"], shot["lighting"],
        ]
        for c in shot["characters"]:
            ch = costume.character(self.doc, c["character_id"])
            want += [ch["name"], ch["appearance"], ch["costume"], c["action"], c["emotion"], f"{ch['age']} 岁中国"]
        for text in want:
            self.assertIn(text, job.prompt)
        self.assertNotIn("$", job.prompt)
        self.assertEqual(job.size, "1152x2048")
        self.assertEqual(job.size, costume.SIZES["portrait"])
        self.assertIn("底部 1/4", job.prompt)
        self.assertIn("不出现任何字幕", job.prompt)
        self.assertIn("静止姿态", job.prompt)
        for zh in ("中景", "平视", "室外", "夜晚"):
            self.assertIn(zh, job.prompt)

    def test_text_scheme_has_no_reference_text_and_no_blank_lines(self):
        job = self.one("text", SOLO)
        self.assertEqual(job.refs, [])
        self.assertNotIn("参考图", job.prompt)
        self.assertNotIn("\n\n", job.prompt)

    def test_templates_sections(self):
        self.assertEqual(set(costume.load_templates(keyframe.PROMPT_VERSION)), {"shot", "character", "reference", "empty"})

    def test_reference_counts_per_scheme(self):
        for scheme, solo, pair in (("text", 0, 0), ("ref1", 1, 2), ("ref2", 2, 4)):
            with self.subTest(scheme=scheme):
                self.assertEqual(len(self.one(scheme, SOLO).refs), solo)
                self.assertEqual(len(self.one(scheme, PAIR).refs), pair)

    def test_two_character_image_order_ref1(self):
        # shot.characters 里陆沉在前，但图序固定按 characters[]：苏晚在前
        job = self.one("ref1", PAIR_LU_FIRST)
        self.assertEqual([r.sha256 for r in job.refs], [self.library[SUWAN]["main"].sha256, self.library[LUCHEN]["main"].sha256])
        self.assertIn("图1是苏晚的全身定妆照，图2是陆沉的全身定妆照", job.prompt)
        self.assertIn("只用于确定人物的身份", job.prompt)
        self.assertIn("场景、姿态、构图、服装一律以文字描述为准", job.prompt)
        self.assertEqual(job.characters, [LUCHEN, SUWAN])  # 文字描述仍按镜头里的顺序
        self.assertLess(job.prompt.index("- 陆沉"), job.prompt.index("- 苏晚"))

    def test_two_character_image_order_ref2(self):
        job = self.one("ref2", PAIR_LU_FIRST)
        lib = self.library
        self.assertEqual(
            [r.sha256 for r in job.refs],
            [lib[SUWAN]["main"].sha256, lib[SUWAN]["neutral"].sha256, lib[LUCHEN]["main"].sha256, lib[LUCHEN]["neutral"].sha256],
        )
        self.assertIn("图1是苏晚的全身定妆照，图2是苏晚的正面特写，图3是陆沉的全身定妆照，图4是陆沉的正面特写", job.prompt)

    def test_single_character_mapping(self):
        self.assertIn("图1是苏晚的全身定妆照。", self.one("ref1", SOLO).prompt)
        self.assertIn("图1是苏晚的全身定妆照，图2是苏晚的正面特写。", self.one("ref2", SOLO).prompt)

    def test_names_not_hardcoded(self):
        doc = copy.deepcopy(self.doc)
        for c in doc["characters"]:
            c["name"] = {SUWAN: "甲", LUCHEN: "乙"}[c["id"]]
        (job,) = keyframe.plan_jobs(doc, ["ref1"], shots=[PAIR], library=self.library)
        self.assertIn("图1是甲的全身定妆照，图2是乙的全身定妆照", job.prompt)
        self.assertIn("- 甲（26 岁中国女性）", job.prompt)  # 人物行与参考图说明用 characters[].name；剧情文字（description / action）原样取自 ep01
        self.assertNotIn("- 苏晚", job.prompt)


def sh_in(scene, shot_id):
    return any(sh["shot_id"] == shot_id for sh in scene["shots"])


class EmptyShotTest(Base):
    def test_empty_shot_shared_once_across_schemes(self):
        jobs = self.jobs(list(keyframe.SCHEMES))
        self.assertEqual(len(jobs), 1 + 12 * 3)
        empties = [j for j in jobs if j.shot_id == EMPTY]
        self.assertEqual([(j.scheme, j.refs, j.characters) for j in empties], [(keyframe.SHARED, [], [])])
        self.assertEqual(jobs[0].shot_id, EMPTY)  # 空镜在前
        self.assertEqual(len(self.jobs(["text"])), 13)
        self.assertEqual(len(self.jobs(["ref2"], [EMPTY])), 1)
        self.assertEqual(self.jobs(["ref1"], [EMPTY])[0].refs, [])  # 空镜从不带参考图

    def test_empty_prompt_describes_scene_only(self):
        job = self.jobs(["text"], [EMPTY])[0]
        self.assertIn("没有任何人物", job.prompt)
        self.assertIn("空无一人", job.prompt)  # 来自 shot.description
        self.assertIn("底部 1/4", job.prompt)
        self.assertNotIn("出镜人物", job.prompt)
        self.assertNotIn("参考图", job.prompt)
        self.assertEqual(job.measurable, keyframe.MEASURABLE_NO)

    def test_empty_shot_request_is_identical_for_every_scheme_set(self):
        a = self.jobs(["text"], [EMPTY])[0]
        b = self.jobs(["text", "ref1", "ref2"], [EMPTY])[0]
        self.assertEqual(costume.node_key("m", a, "r1"), costume.node_key("m", b, "r1"))


class MeasurabilityTest(Base):
    def test_ep01_annotations(self):
        got = {j.shot_id: (j.measurable, j.measurable_reason) for j in self.jobs(["text"])}
        yes = [k for k, v in got.items() if v[0] == "yes"]
        self.assertEqual(len(got), 13)
        self.assertEqual(got[EMPTY][0], "no")
        self.assertEqual(got[FAR][0], "maybe")
        self.assertEqual(got[HAND][0], "maybe")
        self.assertEqual(len(yes), 10)
        self.assertIn("MLS", got[FAR][1])
        self.assertIn("手", got[HAND][1])

    def test_rules_are_data_driven_not_shot_id_driven(self):
        doc = copy.deepcopy(self.doc)
        for n, (_, shot) in enumerate(keyframe.shots_of(doc)):
            shot["shot_id"] = f"renamed_{n:02d}"  # 镜头 id 全换掉，标注应不变
        got = [j.measurable for j in keyframe.plan_jobs(doc, ["text"])]
        want = [j.measurable for j in self.jobs(["text"])]
        self.assertEqual(got, want)

    def test_rule_table(self):
        shot = lambda size, angle, action="站着", chars=1: {  # noqa: E731
            "framing": {"shot_size": size, "angle": angle}, "description": "d",
            "characters": [{"character_id": SUWAN, "action": action, "emotion": "e"}] * chars,
        }
        self.assertEqual(keyframe.measurability(shot("CU", "eye_level"))[0], "yes")
        self.assertEqual(keyframe.measurability(shot("MCU", "low", chars=2))[0], "yes")
        self.assertEqual(keyframe.measurability(shot("LS", "eye_level"))[0], "maybe")
        self.assertEqual(keyframe.measurability(shot("ELS", "eye_level"))[0], "maybe")
        self.assertEqual(keyframe.measurability(shot("MS", "overhead"))[0], "maybe")
        self.assertEqual(keyframe.measurability(shot("ECU", "eye_level"))[0], "maybe")
        self.assertEqual(keyframe.measurability(shot("MS", "eye_level", action="撕胶带（只露出手部）"))[0], "maybe")
        self.assertEqual(keyframe.measurability(shot("CU", "eye_level", chars=0)), ("no", "空镜"))


class SchemeParsingTest(unittest.TestCase):
    def test_parse_schemes(self):
        self.assertEqual(keyframe.parse_schemes("ref2"), ["ref2"])
        self.assertEqual(keyframe.parse_schemes("ref2, text"), ["text", "ref2"])
        self.assertEqual(keyframe.parse_schemes("all"), ["text", "ref1", "ref2"])
        for bad in ("", "ref3", "text,x"):
            with self.assertRaises(ValueError):
                keyframe.parse_schemes(bad)


class RunTest(Base):
    def run_kf(self, settings, gen, env=ENV):
        settings.ref_root = settings.ref_root or str(self.root)
        out = io.StringIO()
        code = keyframe.run_keyframe(settings, env=env, base_dir=self.base / "runs", out=out, gen_fn=gen, sleep=lambda s: None)
        run_dir = next((self.base / "runs").iterdir()) if (self.base / "runs").exists() else None
        return code, run_dir, out.getvalue()

    def load(self, run_dir):
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        calls = [json.loads(ln) for ln in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        return summary, calls

    def test_ref2_pair_shot_sends_four_refs_and_is_priced(self):
        gen = FakeGen()
        code, run_dir, _ = self.run_kf(keyframe.Settings(["ref2"], [PAIR]), gen)
        self.assertEqual(code, 0)
        self.assertEqual(len(gen.calls), 1)
        call = gen.calls[0]
        self.assertEqual(len(call["refs"]), 4)
        self.assertTrue(all(u.startswith("data:image/jpeg;base64,") for u in call["refs"]))
        self.assertEqual((call["size"], call["watermark"], call["model"], call["output_format"]), ("1152x2048", False, seedream.MODELS["pro"], "jpeg"))
        summary, calls = self.load(run_dir)
        self.assertAlmostEqual(summary["cost_cny_total"], 0.30 + 3 * 0.02)  # 第 2 张参考图起每张 ¥0.02
        self.assertAlmostEqual(calls[0]["cost_cny"], pricing.image_cny("ark", seedream.MODELS["pro"], 1152 * 2048, 4))
        self.assertEqual(calls[0]["extra"]["refs"], [r["sha256"] for r in summary["items"][0]["refs"]])
        self.assertEqual((calls[0]["extra"]["shot_id"], calls[0]["extra"]["scheme"]), (PAIR, "ref2"))

    def test_prices_per_scheme(self):
        for scheme, shot, want in (("text", PAIR, 0.30), ("ref1", SOLO, 0.30), ("ref1", PAIR, 0.32), ("ref2", SOLO, 0.32), (keyframe.SHARED, EMPTY, 0.30)):
            with self.subTest(scheme=scheme, shot=shot):
                job = self.jobs(["text" if scheme == keyframe.SHARED else scheme], [shot])[0]
                self.assertAlmostEqual(pricing.image_cny("ark", seedream.MODELS["pro"], pricing.size_pixels(job.size), len(job.refs)), want)

    def test_full_plan_cost_per_round(self):
        total = sum(pricing.image_cny("ark", seedream.MODELS["pro"], pricing.size_pixels(j.size), len(j.refs)) for j in self.jobs(list(keyframe.SCHEMES)))
        self.assertAlmostEqual(total, 11.46)

    def test_summary_calls_and_items(self):
        gen = FakeGen()
        code, run_dir, _ = self.run_kf(keyframe.Settings(["text", "ref1"], [EMPTY, PAIR, SOLO], rounds=2), gen)
        self.assertEqual(code, 0)
        summary, calls = self.load(run_dir)
        self.assertEqual((summary["ok"], summary["n_expected"], summary["n_attempted"]), (10, 10, 10))  # 每轮：空镜 1 + 2 镜头 × 2 方案
        self.assertEqual(len(gen.calls), 10)
        self.assertAlmostEqual(sum(c["cost_cny"] for c in calls), summary["spent_cny_total"])
        self.assertTrue(all(c["provider"] == "ark" and c["capability"] == "image" and c["cost_basis"] == "estimate" for c in calls))
        self.assertTrue(all(c["extra"]["billing"] == "plan" for c in calls))
        self.assertEqual(set(summary["per_scheme"]), {"shared", "text", "ref1"})
        self.assertEqual(set(summary["per_shot_per_round"]), {EMPTY, PAIR, SOLO})
        self.assertNotIn("per_char_per_round", summary)
        self.assertIn("不实付", summary["price"]["note"])
        job = self.one("ref1", PAIR)
        item = next(i for i in summary["items"] if i["shot_id"] == PAIR and i["scheme"] == "ref1" and i["round"] == 2)
        self.assertEqual(item["node_key"], costume.node_key(seedream.MODELS["pro"], job, "r2"))
        self.assertEqual(len({i["node_key"] for i in summary["items"]}), 10)  # 方案、轮次不同，请求内容不同
        self.assertEqual(item["characters"], job.characters)
        self.assertEqual(item["measurable"], "yes")
        self.assertEqual((run_dir / item["file"]).name, "keyframe-ref1.jpg")
        self.assertEqual(Path(item["file"]).parts[-2], PAIR)
        raw = (run_dir / "calls.jsonl").read_text(encoding="utf-8") + (run_dir / "summary.json").read_text(encoding="utf-8")
        self.assertNotIn("base64", raw)
        self.assertNotIn(SENTINEL, raw)
        self.assertTrue(all(len(json.dumps(c)) < costume.MAX_CALL_LINE_BYTES for c in calls))

    def test_cost_limit_aborts_before_request(self):
        gen = FakeGen()
        code, run_dir, out = self.run_kf(keyframe.Settings(["text"], max_cost_cny=0.65), gen)
        self.assertEqual(code, 1)
        self.assertEqual(len(gen.calls), 2)
        summary, _ = self.load(run_dir)
        self.assertIn("上限", summary["aborted"])
        self.assertLessEqual(summary["spent_cny_total"], 0.65)
        self.assertIn("中止", out)

    def test_cost_limit_counts_reference_surcharge(self):
        gen = FakeGen()
        code, _, _ = self.run_kf(keyframe.Settings(["ref2"], [PAIR], max_cost_cny=0.35), gen)  # 4 张参考图 ¥0.36 > 0.35
        self.assertEqual(code, 1)
        self.assertEqual(gen.calls, [])

    def test_account_level_error_aborts(self):
        err = seedream.ImageError("http", "HTTP 403 code=AccountOverdueError", http_status=403, api_code="AccountOverdueError")
        gen = FakeGen(None, err)
        code, run_dir, _ = self.run_kf(keyframe.Settings(["text"]), gen)
        self.assertEqual(code, 1)
        self.assertEqual(len(gen.calls), 2)
        summary, _ = self.load(run_dir)
        self.assertIn("账号级错误", summary["aborted"])

    def test_transient_retry_and_moderation(self):
        gen = FakeGen(seedream.ImageError("http", "HTTP 500", http_status=500), None,
                      seedream.ImageError("moderation", "x", http_status=400, api_code="InputTextSensitiveContentDetected"))
        code, run_dir, _ = self.run_kf(keyframe.Settings(["text"], [SOLO, PAIR]), gen)
        self.assertEqual(code, 1)
        summary, calls = self.load(run_dir)
        self.assertEqual((summary["ok"], summary["transient_failures"], summary["failures_by_kind"]), (1, 1, {"moderation": 1}))
        self.assertEqual([c["cost_cny"] for c in calls], [0.30, 0.30, 0.0])

    def test_missing_reference_images_is_an_input_error(self):
        gen = FakeGen()
        for scheme in ("ref1", "ref2"):
            with self.subTest(scheme=scheme):
                settings = keyframe.Settings([scheme], [SOLO], ref_root=str(self.base / "nowhere"))
                out = io.StringIO()
                code = keyframe.run_keyframe(settings, env=ENV, base_dir=self.base / "runs", out=out, gen_fn=gen)
                self.assertEqual(code, 2)
                self.assertIn("缺少 P0-06 manifest", out.getvalue())
        self.assertEqual(gen.calls, [])
        self.assertFalse((self.base / "runs").exists())  # 输入有误时不创建运行目录
        next((self.root / "derive-ref-pro" / SUWAN).glob("expression-neutral-*.jpg")).unlink()
        out = io.StringIO()
        code = keyframe.run_keyframe(keyframe.Settings(["ref2"], [SOLO], ref_root=str(self.root)), env=ENV, base_dir=self.base / "runs", out=out, gen_fn=gen)
        self.assertEqual(code, 2)
        self.assertIn("参考图不可用", out.getvalue())
        # text 方案不需要参考图，照常运行
        code, _, _ = self.run_kf(keyframe.Settings(["text"], [SOLO], ref_root=str(self.base / "nowhere")), gen)
        self.assertEqual(code, 0)

    def test_input_errors(self):
        cases = [
            (keyframe.Settings(["text"], ["ep01_nope"]), ENV, "未知镜头"),
            (keyframe.Settings(["text"], rounds=0), ENV, "--rounds"),
            (keyframe.Settings(["nope"]), ENV, "--scheme"),
            (keyframe.Settings(["text"]), {}, "ARK_API_KEY"),
            (keyframe.Settings(["text"]), {**ENV, "ARK_BILLING": "free"}, "ARK_BILLING"),
            (keyframe.Settings(["text"], model="flash"), ENV, "Agent Plan 不支持"),
        ]
        for settings, env, msg in cases:
            with self.subTest(msg=msg):
                out = io.StringIO()
                code = keyframe.run_keyframe(settings, env=env, base_dir=self.base / "r", out=out, gen_fn=FakeGen())
                self.assertEqual(code, 2)
                self.assertIn(msg, out.getvalue())


class DryRunTest(Base):
    def test_dry_run_sends_nothing_needs_no_key_and_creates_no_run(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"POC_RUNS_DIR": str(self.base / "runs")}), mock.patch.object(seedream, "generate") as gen:
            code = keyframe.dry_run(keyframe.Settings(["ref2"], [PAIR_LU_FIRST, EMPTY], ref_root=str(self.root)), env={}, out=out)
        self.assertEqual(code, 0)
        gen.assert_not_called()
        self.assertFalse((self.base / "runs").exists())
        text = out.getvalue()
        job = self.one("ref2", PAIR_LU_FIRST)
        self.assertIn(costume.node_key(seedream.MODELS["pro"], job, "r1"), text)
        self.assertIn("预计费用: ¥0.36（1152x2048，参考图 4 张）", text)
        self.assertIn("预计费用: ¥0.30（1152x2048，参考图 0 张）", text)
        for n, r in enumerate(job.refs, 1):
            self.assertIn(f"图{n}: ", text)
            self.assertIn(r.sha256, text)
            self.assertIn(r.path, text)
        self.assertIn(job.prompt.splitlines()[0], text)
        self.assertIn("图1是苏晚的全身定妆照，图2是苏晚的正面特写，图3是陆沉的全身定妆照，图4是陆沉的正面特写", text)
        self.assertIn("每轮 2 张，预计 ¥0.66", text)
        self.assertNotIn("base64", text)

    def test_dry_run_warns_when_over_budget_and_is_deterministic(self):
        s = keyframe.Settings(list(keyframe.SCHEMES), rounds=2, max_cost_cny=5.0, ref_root=str(self.root))
        outs = []
        for _ in range(2):
            out = io.StringIO()
            self.assertEqual(keyframe.dry_run(s, env={}, out=out), 0)
            outs.append(out.getvalue())
        self.assertEqual(outs[0], outs[1])
        self.assertIn("每轮 37 张", outs[0])
        self.assertIn("超过 --max-cost-cny", outs[0])

    def test_dry_run_input_errors(self):
        for settings, msg in ((keyframe.Settings(["ref1"], ref_root=str(self.base / "x")), "缺少 P0-06 manifest"), (keyframe.Settings(["text"], ["nope"]), "未知镜头")):
            out = io.StringIO()
            self.assertEqual(keyframe.dry_run(settings, env={}, out=out), 2)
            self.assertIn(msg, out.getvalue())


class OfflineTest(Base):
    def setUp(self):
        super().setUp()
        out = io.StringIO()
        settings = keyframe.Settings(list(keyframe.SCHEMES), [EMPTY, SOLO, PAIR], rounds=2, ref_root=str(self.root))
        code = keyframe.run_keyframe(settings, env=ENV, base_dir=self.base / "runs", out=out, gen_fn=FakeGen(), sleep=lambda s: None)
        assert code == 0, out.getvalue()
        self.run_dir = next((self.base / "runs").iterdir())
        self.dest = self.base / "evidence" / "keyframe-all"
        self.assertEqual(keyframe.export_run(self.run_dir, self.dest, out=io.StringIO()), 0)
        self.manifest_path = self.dest / "manifest.json"

    def manifest(self):
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def save(self, manifest):
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def verify(self, **kw):
        out = io.StringIO()
        return keyframe.run_verify(self.base / "evidence", out=out, **kw), out.getvalue()

    def test_export_contents(self):
        m = self.manifest()
        self.assertEqual((m["stage"], m["round"], m["watermark"], m["schemes"]), ("keyframe", 1, False, ["text", "ref1", "ref2"]))
        self.assertEqual(len(m["images"]), 1 + 3 * 2)  # 空镜 1 + 两个带角色镜头 × 三方案
        self.assertEqual(m["source"]["sha256"], hashlib.sha256(costume.script.SAMPLE_EP01.read_bytes()).hexdigest())
        for e in m["images"]:
            self.assertEqual(e["round"], 1)
            self.assertTrue(e["file"].endswith(f"{e['sha256'][:8]}.jpg"))
            self.assertEqual((e["width"], e["height"]), (1152, 2048))
            self.assertIs(e["selected"], False)
            self.assertEqual(len(e["node_key"]), 64)
            self.assertTrue(set(e) >= {"shot_id", "characters", "scheme", "measurable", "measurable_reason", "refs", "request_id"})
        by = {(e["shot_id"], e["scheme"]): e for e in m["images"]}
        self.assertEqual(by[(EMPTY, "shared")]["characters"], [])
        self.assertEqual(by[(EMPTY, "shared")]["measurable"], "no")
        self.assertEqual([len(by[(PAIR, s)]["refs"]) for s in keyframe.SCHEMES], [0, 2, 4])
        self.assertEqual(by[(PAIR, "ref1")]["refs"][0]["sha256"], self.library[SUWAN]["main"].sha256)
        self.assertEqual(set(m["references"]), {SUWAN, LUCHEN})
        for name in ("run-summary.json", "run-calls.jsonl"):
            self.assertTrue((self.dest / name).is_file())
        # 第 2 轮不导出；同一方案的第 1 轮图像文件字节即 sha256
        for e in m["images"]:
            self.assertEqual(hashlib.sha256((self.dest / e["file"]).read_bytes()).hexdigest(), e["sha256"])

    def test_export_refuses_nonempty_dest_and_incomplete_run(self):
        self.assertEqual(keyframe.export_run(self.run_dir, self.dest, out=io.StringIO()), 2)
        self.assertEqual(keyframe.export_run(self.base, self.base / "new", out=io.StringIO()), 2)

    def test_verify_roundtrip_ok_and_selection_report(self):
        code, text = self.verify()
        self.assertEqual(code, 0, text)
        self.assertIn("7 张图像", text)
        self.assertIn("已选定首帧 0/3 个镜头", text)
        code, text = self.verify(require_selected=True)
        self.assertEqual(code, 1)
        self.assertIn("没有选定首帧", text)
        m = self.manifest()
        seen = set()
        for e in m["images"]:
            if e["shot_id"] not in seen and e["scheme"] in ("shared", "ref1"):
                e["selected"] = True
                seen.add(e["shot_id"])
        self.save(m)
        code, text = self.verify(require_selected=True)
        self.assertEqual(code, 0, text)
        self.assertIn("已选定首帧 3/3", text)

    def test_verify_rejects_two_selected_in_one_shot(self):
        m = self.manifest()
        for e in m["images"]:
            if e["shot_id"] == PAIR:
                e["selected"] = True
        self.save(m)
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("选定了 3 张首帧", text)

    def test_verify_detects_tampering_and_wrong_size(self):
        img = next(self.dest.rglob("*.jpg"))
        img.write_bytes(img.read_bytes() + b"x")
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("sha256", text)

    def test_verify_detects_wrong_dimensions_in_manifest(self):
        m = self.manifest()
        m["images"][0]["width"] = 720
        self.save(m)
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("尺寸", text)

    def test_verify_detects_ref_count_and_empty_shot_inconsistencies(self):
        m = self.manifest()
        pair_ref2 = next(e for e in m["images"] if e["shot_id"] == PAIR and e["scheme"] == "ref2")
        pair_ref2["refs"] = pair_ref2["refs"][:2]
        empty = next(e for e in m["images"] if e["shot_id"] == EMPTY)
        empty["characters"] = [SUWAN]
        self.save(m)
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("有 2 张参考图，方案 ref2 应为 4 张", text)
        self.assertIn("互相矛盾", text)

    def test_verify_detects_changed_reference_file(self):
        victim = next((self.root / "main-pro" / SUWAN).glob("main-02-*.jpg"))
        victim.write_bytes(victim.read_bytes() + b"x")
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("参考图", text)

    def test_verify_detects_missing_file_watermark_and_summary_mismatch(self):
        m = self.manifest()
        (self.dest / m["images"][1]["file"]).unlink()
        m["watermark"] = True
        m["images"].pop()
        self.save(m)
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("缺少", text)
        self.assertIn("watermark", text)
        self.assertIn("成功项不一致", text)

    def test_verify_detects_cost_mismatch_and_base64(self):
        calls = self.dest / "run-calls.jsonl"
        lines = calls.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["cost_cny"] = 5.0
        first["extra"]["leak"] = "data:image/jpeg;base64,AAAA"
        calls.write_text("\n".join([json.dumps(first)] + lines[1:]) + "\n", encoding="utf-8")
        code, text = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("费用合计", text)
        self.assertIn("base64", text)

    def test_exported_evidence_has_no_secret_or_image_data(self):
        raw = (self.dest / "run-calls.jsonl").read_text(encoding="utf-8") + (self.dest / "run-summary.json").read_text(encoding="utf-8")
        self.assertNotIn(SENTINEL, raw)
        self.assertNotIn("base64", raw)


class CliTest(Base):
    def parse(self, argv):
        from poc.__main__ import build_parser

        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            args = build_parser().parse_args(["keyframe", *argv])
            args.func(args)
        return ctx.exception.code

    def test_usage_errors(self):
        for argv in (
            [], ["--scheme", "nope"], ["--scheme", "text", "--export", "a", "b"], ["--verify", "x", "--scheme", "text"],
            ["--export", "a", "b", "--verify", "x"], ["--verify", "x", "--dry-run"], ["--export", "a", "b", "--require-selected"],
            ["--scheme", "text", "--require-selected"], ["--verify", "x", "--rounds", "2"], ["--export", "a", "b", "--ref-root", "r"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.parse(argv), 2)

    def test_dry_run_through_cli(self):
        from poc.__main__ import build_parser

        args = build_parser().parse_args(["keyframe", "--scheme", "ref2", "--shots", f"{PAIR},{SOLO}", "--dry-run", "--ref-root", str(self.root)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(args.func(args), 0)
        self.assertIn("[1/2]", out.getvalue())
        self.assertIn("方案 ref2", out.getvalue())

    def test_shots_are_comma_separated_and_passed_through(self):
        from poc.__main__ import build_parser

        args = build_parser().parse_args(["keyframe", "--scheme", "text,ref1", "--shots", f"{PAIR}, {SOLO}", "--rounds", "2", "--max-cost-cny", "3"])
        with mock.patch.object(keyframe, "run_keyframe", return_value=0) as run:
            self.assertEqual(args.func(args), 0)
        s = run.call_args.args[0]
        self.assertEqual((s.schemes, s.shots, s.rounds, s.max_cost_cny), (["text", "ref1"], [PAIR, SOLO], 2, 3.0))


if __name__ == "__main__":
    unittest.main()
