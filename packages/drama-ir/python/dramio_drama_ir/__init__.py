"""DramaIR 校验与渲染（v0，只用标准库）。

DramaIR 的唯一定义是 packages/drama-ir/schema/ 下的 JSON Schema（INV-02）。
本包只对 dict 做校验，不定义镜像 Schema 的类型；生成类型在 P1-03 引入（见 docs/tech-debt.md TD-001）。
"""

from dramio_drama_ir.report import Issue, Report
from dramio_drama_ir.schema import SCHEMA_ROOT, SchemaError, load_schema, schema_path
from dramio_drama_ir.checks import validate

__all__ = ["Issue", "Report", "SCHEMA_ROOT", "SchemaError", "load_schema", "schema_path", "validate"]
