"""运行目录与调用记录。

每次运行一个目录 runs/<run_id>/：
- meta.json    命令、参数、起止时间、Python 版本（不含任何密钥）
- calls.jsonl  每次模型调用一行：耗时、费用、用量、状态

runs/ 固定在 spikes/poc/runs/（与当前工作目录无关，避免在仓库根目录生成非法顶层目录），
可用 POC_RUNS_DIR 覆盖。
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from poc import config, providers

COST_BASES = ("reported", "estimate", "free")


def runs_dir() -> Path:
    override = os.environ.get("POC_RUNS_DIR")
    return Path(override) if override else config.PROJECT_DIR / "runs"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id(command: str, now: datetime | None = None) -> str:
    """YYYYMMDD-HHMMSS-<命令>-<4 位 hex>，UTC，按字典序即按时间排序。"""
    now = now or _now()
    return f"{now:%Y%m%d-%H%M%S}-{command}-{secrets.token_hex(2)}"


@dataclass
class CallRecord:
    """一次模型调用。调用方在 with 块内填写费用、用量等字段。"""

    run_id: str
    provider: str
    capability: str
    model: str | None = None
    node_key: str | None = None
    started_at: str = ""
    elapsed_ms: float = 0.0
    status: str = "ok"
    error: str | None = None
    cost_cny: float | None = None
    cost_basis: str | None = None  # reported / estimate / free
    usage: dict[str, Any] | None = None
    request_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Run:
    def __init__(
        self,
        command: str,
        args: dict[str, Any] | None = None,
        base_dir: Path | None = None,
        secrets: list[str] | None = None,
    ):
        """secrets：写入 calls.jsonl 前要脱敏的密钥值；默认取当前进程环境中已配置的密钥。"""
        self.command = command
        self._secrets = providers.secret_values(os.environ) if secrets is None else secrets
        self.run_id = new_run_id(command)
        self.dir = (base_dir or runs_dir()) / self.run_id
        self.calls_path = self.dir / "calls.jsonl"
        self._lock = threading.Lock()
        self.meta: dict[str, Any] = {
            "run_id": self.run_id,
            "command": command,
            "args": args or {},
            "started_at": _iso(_now()),
            "ended_at": None,
            "status": "running",
            "python": platform.python_version(),
        }
        self.dir.mkdir(parents=True, exist_ok=False)
        self.calls_path.touch()
        self.write_json("meta.json", self.meta)

    def __enter__(self) -> Run:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finish("error" if exc_type else "ok")

    def finish(self, status: str = "ok") -> None:
        self.meta["ended_at"] = _iso(_now())
        self.meta["status"] = status
        self.write_json("meta.json", self.meta)

    def write_json(self, name: str, obj: Any) -> Path:
        """写出 JSON；与 calls.jsonl 一样先脱敏兜底（错误信息等字段可能意外带上密钥）。"""
        path = self.dir / name
        text = config.redact(json.dumps(obj, ensure_ascii=False, indent=2, default=str), self._secrets)
        path.write_text(text + "\n", encoding="utf-8")
        return path

    @contextmanager
    def call(
        self, provider: str, capability: str, model: str | None = None, node_key: str | None = None
    ) -> Iterator[CallRecord]:
        """记录一次模型调用：计时，追加到 calls.jsonl；异常也会记录，然后原样抛出。"""
        record = CallRecord(
            run_id=self.run_id,
            provider=provider,
            capability=capability,
            model=model,
            node_key=node_key,
            started_at=_iso(_now()),
        )
        t0 = time.monotonic()
        try:
            yield record
        except BaseException as exc:
            record.status = "error"
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
            self._append(record)

    def _append(self, record: CallRecord) -> None:
        # 在 finally 中调用，不能抛异常，否则会覆盖调用本身的异常
        if record.cost_basis is not None and record.cost_basis not in COST_BASES:
            record.extra["invalid_cost_basis"] = True
        try:
            line = json.dumps(asdict(record), ensure_ascii=False, default=str)
        except Exception as exc:  # 例如 usage / extra 中有循环引用
            fallback = {k: getattr(record, k) for k in ("run_id", "provider", "capability", "model", "node_key")}
            fallback.update(status=record.status, elapsed_ms=record.elapsed_ms, serialize_error=type(exc).__name__)
            line = json.dumps(fallback, ensure_ascii=False, default=str)
        line = config.redact(line, self._secrets)
        with self._lock, self.calls_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
