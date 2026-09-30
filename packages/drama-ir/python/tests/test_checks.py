import unittest

from dramio_drama_ir import validate
from dramio_drama_ir.checks import CHARS_PER_SECOND, speech_chars

from .helpers import lines, sample, shots

SH = "$.episodes[0].scenes[0].shots"


def errors(doc):
    return [(i.path, i.message) for i in validate(doc).errors]


def warnings(doc):
    return [(i.path, i.message) for i in validate(doc).warnings]


class SampleTest(unittest.TestCase):
    """标准样例：0 错误 0 警告，规模符合 P0-02 验收标准。"""

    def test_sample_strict_clean(self):
        report = validate(sample())
        self.assertEqual((report.errors, report.warnings), ([], []))
        self.assertTrue(report.ok(strict=True))

    def test_sample_size(self):
        doc = sample()
        self.assertEqual(len(doc["characters"]), 2)
        self.assertEqual(len(doc["episodes"]), 1)
        self.assertTrue(10 <= len(shots(doc)) <= 14, len(shots(doc)))
        self.assertTrue(13 <= len(lines(doc)) <= 17, len(lines(doc)))
        total = sum(s["duration"]["hint_s"] for s in shots(doc))
        self.assertTrue(50 <= total <= 70, total)
        self.assertTrue(any(not s["characters"] for s in shots(doc)), "至少 1 个无人物镜头")
        self.assertTrue(any(l["kind"] == "voiceover" for l in lines(doc)), "至少 1 句画外音")
        self.assertTrue(any(s["sfx"] for s in shots(doc)), "至少 1 个音效")
        moods = {sc["mood"] for sc in doc["episodes"][0]["scenes"]}
        self.assertGreaterEqual(len(moods), 2)


class ErrorRulesTest(unittest.TestCase):
    def test_unknown_speaker(self):
        doc = sample()
        doc["episodes"][0]["scenes"][0]["shots"][2]["dialogue"][0]["speaker"] = "char_x"
        self.assertEqual(errors(doc), [(f"{SH}[2].dialogue[0].speaker", "引用了不存在的角色 'char_x'")])

    def test_unknown_character_in_shot(self):
        doc = sample()
        doc["episodes"][0]["scenes"][0]["shots"][1]["characters"][0]["character_id"] = "char_x"
        self.assertEqual([p for p, _ in errors(doc)], [f"{SH}[1].characters[0].character_id"])

    def test_duplicate_character_in_shot(self):
        doc = sample()
        shot = doc["episodes"][0]["scenes"][0]["shots"][1]
        shot["characters"].append(dict(shot["characters"][0]))
        self.assertEqual([p for p, _ in errors(doc)], [f"{SH}[1].characters[1].character_id"])

    def test_duplicate_shot_id(self):
        doc = sample()
        doc["episodes"][0]["scenes"][0]["shots"][1]["shot_id"] = "ep01_sc01_sh01"
        ((path, message),) = errors(doc)
        self.assertEqual(path, f"{SH}[1].shot_id")
        self.assertIn(f"{SH}[0].shot_id", message)

    def test_ids_are_unique_across_kinds(self):
        doc = sample()
        doc["episodes"][0]["scenes"][0]["scene_id"] = "ep01"
        self.assertEqual([p for p, _ in errors(doc)], ["$.episodes[0].scenes[0].scene_id"])

    def test_bad_id_format(self):
        doc = sample()
        doc["characters"][0]["id"] = "Char-Suwan"
        # 格式错误，同时所有引用它的地方都变成悬空引用
        self.assertIn(("$.characters[0].id", "id 'Char-Suwan' 格式不合法：应匹配 ^[a-z][a-z0-9_]*$"), errors(doc))

    def test_hint_s_out_of_range(self):
        for bad in (0, -1, 10.5):
            doc = sample()
            doc["episodes"][0]["scenes"][0]["shots"][0]["duration"]["hint_s"] = bad
            self.assertIn(f"{SH}[0].duration.hint_s", [p for p, _ in errors(doc)], bad)

    def test_delivery_ranges(self):
        doc = sample()
        delivery = doc["episodes"][0]["scenes"][0]["shots"][0]["dialogue"][0]["delivery"]
        delivery["intensity"] = 1.5
        delivery["speed"] = 3
        self.assertEqual(
            [p for p, _ in errors(doc)],
            [f"{SH}[0].dialogue[0].delivery.intensity", f"{SH}[0].dialogue[0].delivery.speed"],
        )

    def test_ir_version_major_must_be_zero(self):
        doc = sample()
        doc["ir_version"] = "1.0.0"
        self.assertEqual([p for p, _ in errors(doc)], ["$.ir_version"])

    def test_blank_text(self):
        doc = sample()
        doc["episodes"][0]["scenes"][0]["shots"][0]["dialogue"][0]["text"] = "  "
        doc["episodes"][0]["scenes"][0]["shots"][0]["sfx"][0] = ""
        self.assertEqual(
            [p for p, _ in errors(doc)], [f"{SH}[0].dialogue[0].text", f"{SH}[0].sfx[0]"]
        )

    def test_empty_collections(self):
        doc = sample()
        doc["episodes"][0]["scenes"][2]["shots"] = []
        self.assertIn("$.episodes[0].scenes[2].shots", [p for p, _ in errors(doc)])
        doc = sample()
        doc["episodes"] = []
        self.assertIn("$.episodes", [p for p, _ in errors(doc)])
        doc = sample()
        doc["characters"] = []
        self.assertIn("$.characters", [p for p, _ in errors(doc)])

    def test_episode_number(self):
        doc = sample()
        doc["episodes"][0]["number"] = 0
        self.assertEqual([p for p, _ in errors(doc)], ["$.episodes[0].number"])

    def test_age_range(self):
        doc = sample()
        doc["characters"][0]["age"] = 0
        self.assertEqual([p for p, _ in errors(doc)], ["$.characters[0].age"])


class WarningRulesTest(unittest.TestCase):
    def test_total_duration_deviation(self):
        doc = sample()
        doc["episodes"][0]["target_duration_s"] = 90
        self.assertEqual([p for p, _ in warnings(doc)], ["$.episodes[0].target_duration_s"])
        self.assertEqual(errors(doc), [])
        report = validate(doc)
        self.assertTrue(report.ok())
        self.assertFalse(report.ok(strict=True))

    def test_speech_longer_than_shot(self):
        doc = sample()
        doc["episodes"][0]["scenes"][0]["shots"][0]["dialogue"][0]["text"] = "字" * 30
        self.assertEqual([p for p, _ in warnings(doc)], [f"{SH}[0].duration.hint_s"])

    def test_dialogue_speaker_off_screen(self):
        doc = sample()
        # 镜头 4 只有苏晚在画面里；让陆沉说一句需要口型的台词
        doc["episodes"][0]["scenes"][0]["shots"][3]["dialogue"][0]["speaker"] = "char_luchen"
        self.assertEqual([p for p, _ in warnings(doc)], [f"{SH}[3].dialogue[0].speaker"])
        doc["episodes"][0]["scenes"][0]["shots"][3]["dialogue"][0]["kind"] = "voiceover"
        self.assertEqual(warnings(doc), [])

    def test_unused_character(self):
        doc = sample()
        extra = dict(doc["characters"][1], id="char_extra", name="路人")
        doc["characters"].append(extra)
        self.assertEqual([p for p, _ in warnings(doc)], ["$.characters[2]"])


class SpeechEstimateTest(unittest.TestCase):
    def test_punctuation_not_counted(self):
        self.assertEqual(speech_chars("你……你怎么会有这个？"), 8)
        self.assertEqual(speech_chars("U 盘"), 2)
        self.assertGreater(CHARS_PER_SECOND, 0)
