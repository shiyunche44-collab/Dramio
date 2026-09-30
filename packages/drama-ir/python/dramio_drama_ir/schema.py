"""加载 DramaIR Schema，并用只支持结构关键字的子集校验器检查文档。

子集校验器只认识 SUPPORTED_KEYWORDS 中的关键字；Schema 用到其他关键字时直接报错（fail closed），
避免悄悄漏检；Schema 本身写错（未知 type、enum 不是列表、$ref 无法解析等）同样报错。
值域、格式和引用关系由 checks.py 的语义校验负责。

与 JSON Schema 2020-12 一致：integer 接受 1.0 这类整数值的浮点数；另外拒绝 NaN / Infinity（它们不是合法 JSON）。
"""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path
from typing import Any

from dramio_drama_ir.report import Issue

# packages/drama-ir/schema/
SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schema"

SUPPORTED_KEYWORDS = frozenset({
    "$schema", "$id", "$ref", "$defs", "title", "description",
    "type", "properties", "required", "additionalProperties", "items", "enum",
})
# 值是“名字 → 子 Schema”映射的关键字
_MAPS = ("properties", "$defs")
# 值是单个子 Schema 的关键字
_SUBSCHEMAS = ("items",)


class SchemaError(Exception):
    """Schema 本身不合法，或用到了子集校验器不支持的关键字。"""


def schema_path(version: str = "v0") -> Path:
    return SCHEMA_ROOT / version / "drama.schema.json"


@cache
def _load(version: str) -> str:
    text = schema_path(version).read_text(encoding="utf-8")
    check_schema(json.loads(text))
    return text


def load_schema(version: str = "v0") -> dict[str, Any]:
    """返回 Schema 的一份新拷贝（调用方可以随意修改，例如交给 LLM 做结构化输出）。"""
    return json.loads(_load(version))


def check_schema(schema: Any, root: Any = None, path: str = "#") -> None:
    """检查 Schema 只用了支持的关键字，且取值形状正确；否则抛 SchemaError。"""
    root = schema if root is None else root
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: 子 Schema 必须是对象")
    unknown = set(schema) - SUPPORTED_KEYWORDS
    if unknown:
        raise SchemaError(f"{path}: 不支持的关键字 {sorted(unknown)}；子集校验器只支持 {sorted(SUPPORTED_KEYWORDS)}")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not (isinstance(ref, str) and ref.startswith("#/$defs/")):
            raise SchemaError(f"{path}: $ref 只支持 '#/$defs/<名字>'，实际为 {ref!r}")
        if ref[len("#/$defs/"):] not in (root.get("$defs") or {}):
            raise SchemaError(f"{path}: $ref 指向不存在的定义：{ref}")
        siblings = set(schema) - {"$ref", "title", "description"}
        if siblings:
            raise SchemaError(f"{path}: $ref 旁边只允许 title / description，实际还有 {sorted(siblings)}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        bad = [t for t in types if t not in _TYPES]
        if bad or not types:
            raise SchemaError(f"{path}: 未知的 type {bad or schema['type']!r}；支持 {sorted(_TYPES)}")
    for key in ("enum", "required"):
        if key in schema and not isinstance(schema[key], list):
            raise SchemaError(f"{path}: {key} 必须是列表")
    if "required" in schema and not all(isinstance(n, str) for n in schema["required"]):
        raise SchemaError(f"{path}: required 的元素必须是字符串")
    ap = schema.get("additionalProperties")
    if ap is not None and not isinstance(ap, bool):
        raise SchemaError(f"{path}: additionalProperties 只支持 true / false")
    for key in _MAPS:
        if key in schema:
            if not isinstance(schema[key], dict):
                raise SchemaError(f"{path}: {key} 必须是对象")
            for name, sub in schema[key].items():
                check_schema(sub, root, f"{path}/{key}/{name}")
    for key in _SUBSCHEMAS:
        if key in schema:
            check_schema(schema[key], root, f"{path}/{key}")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: _is_number(v) and float(v).is_integer(),
    "number": _is_number,
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}
_TYPE_NAMES = {dict: "object", list: "array", str: "string", bool: "boolean", int: "integer", float: "number"}


def structural_issues(doc: Any, schema: dict[str, Any]) -> list[Issue]:
    check_schema(schema)
    out: list[Issue] = []
    _check(doc, schema, schema, "$", out)
    return out


def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    name = ref[len("#/$defs/"):]
    try:
        return root["$defs"][name]
    except KeyError:
        raise SchemaError(f"$ref 指向不存在的定义：{ref}") from None


def _check(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str, out: list[Issue]) -> None:
    if "$ref" in schema:
        _check(value, _resolve(schema["$ref"], root), root, path, out)
        return
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPES[t](value) for t in types):
            actual = "null" if value is None else _TYPE_NAMES.get(type(value), type(value).__name__)
            if isinstance(value, float) and not math.isfinite(value):
                actual = repr(value)
            out.append(Issue(path, f"类型应为 {'/'.join(types)}，实际为 {actual}"))
            return
    if "enum" in schema and value not in schema["enum"]:
        out.append(Issue(path, f"取值 {value!r} 不在允许范围内：{schema['enum']}"))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                out.append(Issue(path, f"缺少必需字段 '{name}'"))
        for name, sub in value.items():
            if name in props:
                _check(sub, props[name], root, f"{path}.{name}", out)
            elif schema.get("additionalProperties") is False:
                out.append(Issue(f"{path}.{name}", f"不允许的字段 '{name}'"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _check(item, schema["items"], root, f"{path}[{i}]", out)
