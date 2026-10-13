"""node_key：节点输入的内容哈希（P0-12，验证 ADR-0006 / INV-05 的设计）。

key = sha256(规范化 JSON {kind, version, params, template_sha256, ir, upstream})：
- kind / version：节点类型与实现版本；
- params：模型、尺寸、采样序号等一切影响结果的参数；
- template_sha256：Prompt 模板文件内容的哈希（改了模板就失效，不只看版本号）；
- ir：该节点用到的 DramaIR 字段（只放相关字段，不放整份文档，这样别处的改动不会让它失效）；
- upstream：上游产物的 sha256（不是上游 key：上游重算出相同内容时下游仍然命中）。
不含 run_id、时间戳、绝对路径。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def sha256_json(obj: Any) -> str:
    return sha256_bytes(canonical(obj).encode("utf-8"))


def node_key(
    kind: str,
    version: str,
    params: Mapping[str, Any],
    *,
    template_sha256: str | None = None,
    ir: Any = None,
    upstream: Sequence[str] = (),
) -> str:
    bad = [u for u in upstream if not SHA_RE.match(u)]
    if bad:
        raise ValueError(f"upstream 必须是产物的 sha256：{bad[:1]}")
    payload = {
        "kind": kind, "version": version, "params": dict(params), "template_sha256": template_sha256,
        "ir": ir, "upstream": list(upstream),
    }
    return sha256_json(payload)
