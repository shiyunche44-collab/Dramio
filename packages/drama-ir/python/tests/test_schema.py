import unittest

from dramio_drama_ir import SchemaError, load_schema, validate
from dramio_drama_ir.schema import SUPPORTED_KEYWORDS, check_schema

from .helpers import sample


def paths(report):
    return [i.path for i in report.errors]


class SchemaSelfCheckTest(unittest.TestCase):
    def test_schema_uses_only_supported_keywords(self):
        check_schema(load_schema("v0"))  # 不抛异常

    def test_unsupported_keyword_fails_closed(self):
        for key, value in (("minimum", 0), ("pattern", "^x$"), ("oneOf", []), ("format", "uri")):
            schema = load_schema()
            schema["$defs"]["duration"]["properties"]["hint_s"][key] = value
            with self.assertRaises(SchemaError, msg=key):
                validate(sample(), schema)

    def test_only_local_refs(self):
        schema = load_schema()
        schema["properties"]["series"] = {"$ref": "https://example.com/other.json"}
        with self.assertRaises(SchemaError):
            check_schema(schema)

    def test_malformed_schema_fails_closed(self):
        cases = {
            "未知 type": lambda s: s["$defs"]["duration"]["properties"]["hint_s"].update(type="float"),
            "enum 不是列表": lambda s: s["$defs"]["series"]["properties"]["aspect_ratio"].update(enum="9:16 16:9"),
            "required 不是列表": lambda s: s["$defs"]["duration"].update(required="hint_s"),
            "properties 不是对象": lambda s: s["$defs"]["duration"].update(properties=[]),
            "$ref 无法解析": lambda s: s["properties"].update(series={"$ref": "#/$defs/nope"}),
            "$ref 旁有其他关键字": lambda s: s["properties"]["series"].update(type="object"),
        }
        for name, mutate in cases.items():
            schema = load_schema()
            mutate(schema)
            with self.assertRaises(SchemaError, msg=name):
                check_schema(schema)

    def test_load_schema_returns_copy(self):
        load_schema()["title"] = "changed"
        self.assertEqual(load_schema()["title"], "DramaIR v0")

    def test_every_object_is_closed_and_fully_required(self):
        """便于直接交给 LLM 做严格结构化输出：所有对象都关闭多余字段，所有字段都必填。"""
        schema = load_schema()
        objects = [schema, *schema["$defs"].values()]
        for obj in objects:
            if obj.get("type") == "object":
                self.assertIs(obj["additionalProperties"], False, obj.get("title"))
                self.assertEqual(sorted(obj["required"]), sorted(obj["properties"]), obj.get("title"))
        self.assertTrue(SUPPORTED_KEYWORDS)


class StructuralTest(unittest.TestCase):
    def test_sample_is_valid(self):
        self.assertEqual(validate(sample()).errors, [])

    def test_missing_required_field(self):
        doc = sample()
        del doc["episodes"][0]["scenes"][1]["shots"][2]["framing"]
        report = validate(doc)
        self.assertEqual(paths(report), ["$.episodes[0].scenes[1].shots[2]"])
        self.assertIn("framing", report.errors[0].message)

    def test_extra_field(self):
        doc = sample()
        doc["characters"][0]["nickname"] = "晚晚"
        self.assertEqual(paths(validate(doc)), ["$.characters[0].nickname"])

    def test_invalid_enum(self):
        doc = sample()
        doc["episodes"][0]["scenes"][0]["shots"][0]["framing"]["shot_size"] = "WIDE"
        self.assertEqual(paths(validate(doc)), ["$.episodes[0].scenes[0].shots[0].framing.shot_size"])

    def test_wrong_types(self):
        doc = sample()
        doc["characters"][0]["age"] = True  # bool 不是 integer
        doc["characters"][1]["age"] = 32.5
        doc["episodes"][0]["scenes"][0]["shots"][0]["duration"]["hint_s"] = "4"
        doc["episodes"][0]["scenes"][0]["shots"][0]["sfx"] = "雨声"
        self.assertEqual(
            paths(validate(doc)),
            [
                "$.characters[0].age",
                "$.characters[1].age",
                "$.episodes[0].scenes[0].shots[0].duration.hint_s",
                "$.episodes[0].scenes[0].shots[0].sfx",
            ],
        )

    def test_integer_accepts_integral_float_like_json_schema(self):
        doc = sample()
        doc["episodes"][0]["number"] = 1.0
        self.assertEqual(validate(doc).errors, [])

    def test_nan_and_infinity_are_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            doc = sample()
            doc["episodes"][0]["target_duration_s"] = bad
            self.assertEqual(paths(validate(doc)), ["$.episodes[0].target_duration_s"], bad)

    def test_root_not_object(self):
        self.assertEqual(paths(validate([])), ["$"])

    def test_structural_errors_skip_semantic_checks(self):
        doc = sample()
        del doc["characters"]
        report = validate(doc)
        self.assertEqual(paths(report), ["$"])
        self.assertEqual(report.warnings, [])
