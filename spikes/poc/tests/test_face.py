import contextlib
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from poc import face
from poc.__main__ import build_parser
from tests.test_seedream import jpeg

A, B = [1.0, 0.0], [0.0, 1.0]  # 两个互相正交的“身份”
TH = face.Thresholds()


def mkface(x=0, size=100.0, score=0.9, emb=A):
    return face.Face((x, 10.0, x + size, 10.0 + size), score, list(emb))


def unit(deg):
    r = math.radians(deg)
    return [math.cos(r), math.sin(r)]


class FakeEmbedder:
    """按图像字节内容返回预设的人脸（不需要 numpy / insightface）。"""

    name = "fake"

    def __init__(self):
        self.faces: dict[bytes, list[face.Face]] = {}
        self.calls = 0

    def add(self, tag: str, *faces: face.Face) -> bytes:
        data = jpeg(64, 64, payload=tag.encode())
        self.faces[data] = list(faces)
        return data

    def detect(self, image_bytes):
        self.calls += 1
        return list(self.faces[image_bytes])


class SimilarityTest(unittest.TestCase):
    def test_cosine(self):
        self.assertAlmostEqual(face.cosine(A, A), 1.0)
        self.assertAlmostEqual(face.cosine(A, B), 0.0)
        self.assertAlmostEqual(face.cosine(A, [-1.0, 0.0]), -1.0)
        self.assertAlmostEqual(face.cosine([3.0, 4.0], [6.0, 8.0]), 1.0)  # 不要求输入已归一化
        self.assertEqual(face.cosine([0.0, 0.0], A), 0.0)

    def test_face_geometry_and_filters(self):
        f = face.Face((10, 20, 110, 70), 0.9, A)
        self.assertEqual((f.width, f.height, f.min_side), (100, 50, 50))
        self.assertIsNone(face.face_reason(f, TH))
        self.assertEqual(face.face_reason(mkface(size=39.9), TH), face.SMALL_FACE)
        self.assertIsNone(face.face_reason(mkface(size=40), TH))  # 短边恰为阈值可度量
        self.assertEqual(face.face_reason(mkface(score=0.49), TH), face.LOW_SCORE)
        self.assertIsNone(face.face_reason(mkface(score=0.5), TH))
        self.assertEqual(face.face_reason(mkface(size=30), face.Thresholds(min_face_px=20)), None)
        self.assertEqual(face.face_reason(mkface(score=0.6), face.Thresholds(min_det_score=0.7)), face.LOW_SCORE)


class BankTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fe = FakeEmbedder()
        self.refs = {}
        for cid, anchor, extra in (("A", A, unit(60)), ("B", B, None)):
            paths = []
            for n, emb in enumerate([anchor] + ([extra] if extra else [])):
                p = self.dir / f"{cid}{n}.jpg"
                p.write_bytes(self.fe.add(f"ref-{cid}-{n}", mkface(emb=emb)))
                paths.append(p)
            self.refs[cid] = paths

    def tearDown(self):
        self.tmp.cleanup()

    def test_anchor_and_bank_metrics(self):
        bank = face.build_bank(self.fe, self.refs, TH)
        self.assertEqual([len(bank.refs["A"]), len(bank.refs["B"])], [2, 1])
        probe = mkface(emb=unit(60))  # 与 A 的补充参考图完全一致，与锚点夹角 60°
        self.assertAlmostEqual(bank.anchor("A", probe), 0.5)
        self.assertAlmostEqual(bank.best("A", probe), 1.0)
        self.assertAlmostEqual(bank.anchor("B", probe), math.sin(math.radians(60)))

    def test_reference_picks_largest_usable_face(self):
        p = self.dir / "multi.jpg"
        p.write_bytes(self.fe.add("multi", mkface(size=50, emb=B), mkface(x=200, size=120, emb=A), mkface(x=400, size=300, score=0.3, emb=B)))
        bank = face.build_bank(self.fe, {"A": [p]}, TH)
        self.assertEqual(bank.refs["A"][0].face.embedding, A)  # 最大的可度量脸（低分的大脸被过滤）

    def test_reference_without_usable_face_is_an_error(self):
        p1, p2 = self.dir / "none.jpg", self.dir / "tiny.jpg"
        p1.write_bytes(self.fe.add("none"))
        p2.write_bytes(self.fe.add("tiny", mkface(size=10)))
        with self.assertRaisesRegex(face.FaceError, "没有检出人脸"):
            face.build_bank(self.fe, {"A": [p1]}, TH)
        with self.assertRaisesRegex(face.FaceError, "脸太小"):
            face.build_bank(self.fe, {"A": [p2]}, TH)
        with self.assertRaisesRegex(face.FaceError, "不可读"):
            face.build_bank(self.fe, {"A": [self.dir / "missing.jpg"]}, TH)


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self.fe = FakeEmbedder()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        refs = {}
        for cid, emb in (("A", A), ("B", B)):
            p = self.dir / f"{cid}.jpg"
            p.write_bytes(self.fe.add(f"ref-{cid}", mkface(emb=emb)))
            refs[cid] = [p]
        self.bank = face.build_bank(self.fe, refs, TH)

    def tearDown(self):
        self.tmp.cleanup()

    def measure(self, tag, faces, expected=("A", "B"), **kw):
        data = self.fe.add(tag, *faces)
        return face.measure_image(self.fe, data, tag, list(expected), self.bank, TH, **kw)

    def by_char(self, result):
        return {i.char_id: i for i in result.instances}

    def test_2x2_assignment_ignores_face_order_and_reports_margin(self):
        # 左边的脸像 B（夹角 10°），右边的脸像 A（夹角 20°）：顺序与角色顺序相反
        r = self.measure("swap", [mkface(0, emb=unit(80)), mkface(300, emb=unit(20))])
        i = self.by_char(r)
        self.assertTrue(i["A"].measurable and i["B"].measurable)
        self.assertEqual(i["A"].bbox[0], 300.0)
        self.assertEqual(i["B"].bbox[0], 0.0)
        self.assertAlmostEqual(i["A"].anchor, math.cos(math.radians(20)), places=4)
        self.assertAlmostEqual(i["B"].bank, math.cos(math.radians(10)), places=4)
        # margin = 对被指派角色的相似度 − 对另一角色的最高相似度
        self.assertAlmostEqual(i["A"].margin, math.cos(math.radians(20)) - math.cos(math.radians(70)), places=4)

    def test_assignment_maximizes_total_not_greedy(self):
        # 两张脸都更像 A，但总相似度最大的指派是 脸1→A、脸2→B
        sims = [[0.9, 0.8], [0.85, 0.1]]
        self.assertEqual(face.best_assignment(sims), [(1, 0), (0, 1)])  # 总 0.85 + 0.8 = 1.65 > 0.9 + 0.1
        self.assertEqual(face.best_assignment([]), [])
        with self.assertRaises(ValueError):
            face.best_assignment([[0.1] * 4])

    def test_margin_is_none_for_single_character(self):
        single = self.measure("one", [mkface(emb=unit(10))], expected=["A"])
        self.assertIsNone(single.instances[0].margin)

    def test_more_faces_than_characters_takes_best(self):
        r = self.measure("crowd", [mkface(0, emb=unit(30)), mkface(200, emb=unit(5)), mkface(400, emb=unit(85)), mkface(600, emb=unit(45))])
        i = self.by_char(r)
        self.assertEqual(r.n_faces, 4)
        self.assertEqual((i["A"].bbox[0], i["B"].bbox[0]), (200.0, 400.0))

    def test_fewer_faces_than_characters_marks_missing(self):
        r = self.measure("one-face", [mkface(emb=unit(80))])
        i = self.by_char(r)
        self.assertTrue(i["B"].measurable)  # 这张脸更像 B
        self.assertFalse(i["A"].measurable)
        self.assertEqual(i["A"].reason, face.MISSED)
        self.assertIsNone(i["A"].anchor)

    def test_unmeasurable_reasons(self):
        none = self.measure("none", [], expected=["A"])
        self.assertEqual((none.n_faces, none.instances[0].reason), (0, face.NO_FACE))
        tiny = self.measure("tiny", [mkface(size=20)], expected=["A"])
        self.assertEqual((tiny.n_faces, tiny.instances[0].measurable, tiny.instances[0].reason), (1, False, face.SMALL_FACE))
        self.assertEqual(tiny.faces[0].reason, face.SMALL_FACE)
        weak = self.measure("weak", [mkface(score=0.3)], expected=["A"])
        self.assertEqual(weak.instances[0].reason, face.LOW_SCORE)
        # 小脸被过滤后，剩下的一张脸照常度量，缺的角色记“未检出”
        mixed = self.measure("mixed", [mkface(size=20, emb=B), mkface(300, emb=A)])
        i = self.by_char(mixed)
        self.assertTrue(i["A"].measurable)
        self.assertEqual(i["B"].reason, face.MISSED)

    def test_empty_expected_only_counts_faces(self):
        r = self.measure("empty-shot", [mkface()], expected=[])
        self.assertEqual((r.n_faces, r.instances), (1, []))

    def test_assign_by_anchor_changes_metric_used(self):
        # A 有补充参考图时 bank ≥ anchor；这里只验证参数被接受且 anchor 口径不看补充图
        p = self.dir / "A2.jpg"
        p.write_bytes(self.fe.add("ref-A2", mkface(emb=unit(60))))
        bank = face.build_bank(self.fe, {"A": [self.dir / "A.jpg", p]}, TH)
        data = self.fe.add("probe", mkface(emb=unit(60)))
        r = face.measure_image(self.fe, data, "probe", ["A"], bank, TH, assign_by="anchor")
        self.assertAlmostEqual(r.instances[0].anchor, 0.5, places=4)
        self.assertAlmostEqual(r.instances[0].bank, 1.0, places=4)

    def test_values_are_rounded_to_four_places(self):
        r = self.measure("round", [mkface(emb=unit(33.3333))], expected=["A"])
        v = r.instances[0].anchor
        self.assertEqual(v, round(v, 4))


class StatsTest(unittest.TestCase):
    def test_describe_known_values(self):
        d = face.describe([0.5, 0.1, 0.3, 0.2, 0.4])
        self.assertEqual((d["n"], d["min"], d["max"], d["median"], d["mean"]), (5, 0.1, 0.5, 0.3, 0.3))
        self.assertEqual((d["p10"], d["p25"], d["p75"], d["p90"]), (0.14, 0.2, 0.4, 0.46))

    def test_describe_edge_cases(self):
        self.assertEqual(face.describe([]), {"n": 0, **dict.fromkeys(("min", "p10", "p25", "median", "p75", "p90", "max", "mean"))})
        one = face.describe([0.7])
        self.assertEqual((one["n"], one["p10"], one["median"], one["p90"]), (1, 0.7, 0.7, 0.7))

    def test_histogram_bins_and_bars(self):
        lines = face.histogram([0.61, 0.62, 0.71, -0.02])
        self.assertEqual(lines[0], "[-0.05, +0.00) " + "█" * 15 + " 1")
        self.assertIn("[+0.60, +0.65) " + "█" * 30 + " 2", lines)
        self.assertIn("[+0.65, +0.70) 0", lines)  # 中间的空箱也列出
        self.assertEqual(lines[-1], "[+0.70, +0.75) " + "█" * 15 + " 1")
        self.assertEqual(face.histogram([]), [])
        self.assertEqual(face.histogram([1.0]), ["[+0.95, +1.00] " + "█" * 30 + " 1"])  # 1.0 归入最后一箱
        self.assertTrue(face.histogram([0.6, 0.6, 0.6] + [0.8] * 100)[0].count("█") >= 1)  # 非空箱至少一格

    def test_group_stats_by_scheme_and_char(self):
        def res(scheme, inst):
            return face.ImageResult("x", 1, [], inst, [i.char_id for i in inst], scheme=scheme)

        ok = lambda c, v: face.Instance(c, True, None, v, v, None)  # noqa: E731
        results = [res("text", [ok("A", 0.4)]), res("ref1", [ok("A", 0.6), face.Instance("B", False, face.MISSED)]), res("ref1", [ok("B", 0.8)])]
        s = face.group_stats(results)
        self.assertEqual((s["overall"]["instances"], s["overall"]["measurable"]), (4, 3))
        self.assertEqual(s["overall"]["measurable_rate"], 0.75)
        self.assertEqual(s["by_scheme"]["ref1"]["bank"]["n"], 2)
        self.assertEqual(s["by_scheme"]["ref1"]["bank"]["median"], 0.7)
        self.assertEqual(s["by_scheme"]["ref1"]["histogram"]["bank"], [[0.6, 1], [0.65, 0], [0.7, 0], [0.75, 0], [0.8, 1]])
        self.assertEqual(s["by_char"]["B"]["measurable"], 1)
        self.assertNotIn("by_scheme", face.group_stats([face.ImageResult("x", 0, [], [ok("A", 0.5)], ["A"])]))


class OutputTest(unittest.TestCase):
    """整条链路（伪造后端）：输出确定，JSON / CSV / 文本逐字节一致。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fe = FakeEmbedder()
        self.refs = {}
        for cid, emb in (("A", A), ("B", B)):
            p = self.dir / "refs" / f"{cid}.jpg"
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(self.fe.add(f"ref-{cid}", mkface(emb=emb)))
            self.refs[cid] = [p]

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, tag, *faces):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.fe.add(tag, *faces))
        return p

    def go(self, targets, **kw):
        out = io.StringIO()
        code = face.run_face(self.refs, targets, self.fe, out=out, **kw)
        return code, out.getvalue()

    def test_directory_inference_and_determinism(self):
        self.write("imgs/A/one.jpg", "a1", mkface(emb=unit(20)))
        self.write("imgs/B/two.jpg", "b1", mkface(emb=unit(70)))
        self.write("imgs/B/back.jpg", "b2")
        self.write("imgs/other/pair.jpg", "p1", mkface(0, emb=unit(10)), mkface(300, emb=unit(80)))
        targets = [face.Target(p, None) for p in face.collect_images([str(self.dir / "imgs")])]
        runs = []
        for n in (1, 2):
            jp, cp = self.dir / f"r{n}.json", self.dir / f"r{n}.csv"
            code, text = self.go(targets, json_path=jp, csv_path=cp)
            self.assertEqual(code, 0, text)
            runs.append((text.replace(str(jp), "J").replace(str(cp), "C"), jp.read_bytes(), cp.read_bytes()))
        self.assertEqual(runs[0], runs[1])
        report = json.loads(runs[0][1])
        by_image = {Path(i["image"]).name: i for i in report["images"]}
        self.assertEqual([x["char_id"] for x in by_image["one.jpg"]["instances"]], ["A"])  # 目录名 = 角色
        self.assertEqual([x["char_id"] for x in by_image["pair.jpg"]["instances"]], ["A", "B"])  # 其它目录 = 基准库全部角色
        self.assertEqual(by_image["back.jpg"]["instances"][0]["reason"], face.NO_FACE)
        self.assertEqual(report["stats"]["overall"]["instances"], 5)
        self.assertEqual(report["stats"]["overall"]["measurable"], 4)
        self.assertNotIn("embedding", runs[0][1].decode())  # 不输出特征向量
        csv_lines = runs[0][2].decode().splitlines()
        self.assertEqual(csv_lines[0], ",".join(face.CSV_COLUMNS))
        self.assertEqual(len(csv_lines), 1 + 5)
        self.assertNotIn("\r", runs[0][2].decode())

    def test_reference_images_in_batch_are_skipped(self):
        self.write("imgs/A/one.jpg", "a1", mkface(emb=unit(20)))
        targets = [face.Target(p, None) for p in face.collect_images([str(self.dir)])]  # 目录里包含参考图本身
        code, text = self.go(targets, json_path=self.dir / "o.json")
        self.assertEqual(code, 0)
        report = json.loads((self.dir / "o.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["skipped_same_as_reference"]), 2)
        self.assertEqual(len(report["images"]), 1)
        self.assertIn("已跳过 2 张", text)

    def test_expect_overrides_directory_name(self):
        p = self.write("imgs/A/cross.jpg", "x1", mkface(emb=B))
        code, text = self.go([face.Target(p, None)], expect=["B"], json_path=self.dir / "o.json")
        self.assertEqual(code, 0)
        inst = json.loads((self.dir / "o.json").read_text(encoding="utf-8"))["images"][0]["instances"]
        self.assertEqual([i["char_id"] for i in inst], ["B"])
        self.assertAlmostEqual(inst[0]["anchor"], 1.0)

    def test_manifest_mode_per_shot_table(self):
        shots = [
            ("s2", "text", ["A", "B"], "yes", [mkface(0, emb=unit(15)), mkface(300, emb=unit(75))]),
            ("s1", "ref1", ["A"], "maybe", [mkface(emb=unit(30))]),
            ("s3", "shared", [], "no", []),
            ("s1", "text", ["A"], "maybe", [mkface(size=20)]),
        ]
        entries = []
        for n, (shot, scheme, chars, pre, faces) in enumerate(shots):
            self.write(f"kf/{shot}/keyframe-{scheme}.jpg", f"kf{n}", *faces)
            entries.append({"shot_id": shot, "characters": chars, "scheme": scheme, "round": 1, "measurable": pre,
                            "file": f"{shot}/keyframe-{scheme}.jpg", "selected": False})
        (self.dir / "kf" / "manifest.json").write_text(json.dumps({"images": entries}), encoding="utf-8")
        targets = face.load_manifests([str(self.dir / "kf" / "manifest.json")])
        self.assertEqual([(t.meta["shot_id"], t.meta["scheme"]) for t in targets], [("s1", "text"), ("s1", "ref1"), ("s2", "text"), ("s3", "shared")])
        code, text = self.go(targets, json_path=self.dir / "m.json", csv_path=self.dir / "m.csv")
        self.assertEqual(code, 0, text)
        self.assertIn("逐镜头", text)
        self.assertIn("脸太小", text)
        self.assertIn(face.EMPTY_SHOT, text)
        self.assertIn("[方案 ref1]", text)
        report = json.loads((self.dir / "m.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(report["stats"]["by_scheme"]), ["ref1", "text"])  # 空镜没有实例，不出现在分组里
        self.assertEqual(report["stats"]["by_scheme"]["text"]["measurable"], 2)  # s1 脸太小不可度量；s2 两个角色可度量
        rows = (self.dir / "m.csv").read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(",s3,shared,1,no,,0,0,空镜," in r for r in rows))  # 空镜：只记脸数，原因“空镜”

    def test_unknown_character_and_bad_input_exit_2(self):
        p = self.write("x.jpg", "x", mkface())
        code, text = self.go([face.Target(p, ["Z"])])
        self.assertEqual(code, 2)
        self.assertIn("基准库里没有", text)
        with self.assertRaises(face.FaceError):
            face.collect_images([str(self.dir / "nope")])
        with self.assertRaises(face.FaceError):
            face.parse_refs(["A"])
        bad = self.dir / "m.json"
        bad.write_text(json.dumps({"images": [{"file": "a.jpg"}]}), encoding="utf-8")
        with self.assertRaisesRegex(face.FaceError, "keyframe 的 manifest"):
            face.load_manifests([str(bad)])


class BackendTest(unittest.TestCase):
    def test_missing_dependencies_give_install_hint(self):
        with mock.patch.dict(sys.modules, {"numpy": None}):
            with self.assertRaises(face.BackendError) as ctx:
                face.ArcFaceEmbedder(model_root="/nonexistent")
        msg = str(ctx.exception)
        self.assertIn("pip install -e '.[face]'", msg)
        self.assertIn("buffalo_l", msg)

    def test_missing_weights_give_download_hint(self):
        fake = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(face.BackendError, "buffalo_l.zip"):
                face.ArcFaceEmbedder(model_root=tmp, importer=lambda name: fake)

    def test_unknown_backend(self):
        with self.assertRaises(face.BackendError):
            face.make_backend("nope")

    def test_cli_reports_missing_dependencies_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "a.jpg"
            img.write_bytes(jpeg(64, 64))
            args = build_parser().parse_args(["face", "--refs", f"A={img}", "--images", str(img)])
            out = io.StringIO()
            with mock.patch.dict(sys.modules, {"numpy": None}), contextlib.redirect_stdout(out):
                code = args.func(args)
        self.assertEqual(code, 2)
        self.assertIn("pip install", out.getvalue())

    def test_module_imports_without_numpy(self):
        # face.py 顶层只用标准库：没装 numpy / insightface 也能 import（依赖只在 ArcFaceEmbedder 构造时加载）
        code = "import sys; sys.modules.update(numpy=None, cv2=None, insightface=None, onnxruntime=None); import poc.face"
        done = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


class CliTest(unittest.TestCase):
    def parse(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            args = build_parser().parse_args(["face", *argv])
            args.func(args)
        return ctx.exception.code

    def test_usage_errors(self):
        for argv in ([], ["--refs", "A=x"], ["--refs", "A=x", "--images", "a", "--manifest", "m.json"],
                     ["--refs", "A=x", "--manifest", "m.json", "--expect", "A"], ["--refs", "A=x", "--images", "a", "--backend", "dlib"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.parse(argv), 2)


if __name__ == "__main__":
    unittest.main()
