"""节点缓存（P0-12）：产物按 sha256 内容寻址（对应 INV-06），节点记录按 node_key 查找。

root/cas/<sha256>               产物字节（原子写入；读取时重新哈希，损坏视为未命中）
root/nodes/<kind>/<key>.json    {kind, key, artifacts: {名字: {sha256, bytes}}, cost_cny, elapsed_s, meta}
失败结果不进缓存；命中时调用方不得再访问供应商。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from poc import nodekey


@dataclass
class Record:
    kind: str
    key: str
    artifacts: dict[str, dict[str, Any]]
    cost_cny: float = 0.0
    elapsed_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class NodeCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def cas_path(self, sha: str) -> Path:
        if not nodekey.SHA_RE.match(sha):
            raise ValueError(f"不是 sha256：{sha!r}")
        return self.root / "cas" / sha

    def put_blob(self, data: bytes) -> str:
        sha = nodekey.sha256_bytes(data)
        p = self.cas_path(sha)
        if self.blob(sha) is None:  # 不存在，或内容已损坏：（重新）原子写入
            _atomic_write(p, data)
        return sha

    def blob(self, sha: str) -> bytes | None:
        p = self.cas_path(sha)
        try:
            data = p.read_bytes()
        except OSError:
            return None
        return data if nodekey.sha256_bytes(data) == sha else None

    def _node_path(self, kind: str, key: str) -> Path:
        if not nodekey.SHA_RE.match(key) or "/" in kind or kind.startswith("."):
            raise ValueError(f"非法的节点 {kind}/{key}")
        return self.root / "nodes" / kind / f"{key}.json"

    def get(self, kind: str, key: str) -> Record | None:
        path = self._node_path(kind, key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            rec = Record(raw["kind"], raw["key"], raw["artifacts"], float(raw.get("cost_cny", 0.0)), float(raw.get("elapsed_s", 0.0)), raw.get("meta", {}))
        except (KeyError, TypeError, ValueError):
            return None
        if rec.kind != kind or rec.key != key:
            return None
        try:
            if not rec.artifacts or any(self.blob(art["sha256"]) is None for art in rec.artifacts.values()):
                return None  # 产物缺失或损坏：当作未命中，重新生成
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return rec

    def put(self, kind: str, key: str, blobs: dict[str, bytes], *, cost_cny: float = 0.0, elapsed_s: float = 0.0, meta: dict[str, Any] | None = None) -> Record:
        arts = {name: {"sha256": self.put_blob(data), "bytes": len(data)} for name, data in blobs.items()}
        rec = Record(kind, key, arts, round(cost_cny, 6), round(elapsed_s, 3), meta or {})
        body = {"kind": kind, "key": key, "artifacts": arts, "cost_cny": rec.cost_cny, "elapsed_s": rec.elapsed_s, "meta": rec.meta}
        _atomic_write(self._node_path(kind, key), (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return rec

    def read(self, rec: Record, name: str) -> bytes:
        data = self.blob(rec.artifacts[name]["sha256"])
        if data is None:
            raise FileNotFoundError(f"缓存产物缺失：{rec.kind}/{rec.key[:8]}/{name}")
        return data
