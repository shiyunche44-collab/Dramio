"""P0-08 账本：把 tasks.jsonl、供应商任务记录（免费查询）和本地视频文件对账，写 ledger.json；离线核验证据目录。

为什么需要它：长任务会跨会话、`--resume` 会反复写 run-summary.json 与 calls.jsonl，费用和成败不能只靠最后一次运行的记录。
账本以供应商返回的 usage 为计费依据，并（可选）重新下载视频比对 sha256，确认“任务 → 镜头 → 本地文件”的对应没有错位。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from poc import media, minimax, pricing

LEDGER = "ledger.json"
SECRET_ENV = ("MINIMAX_API_KEY", "MINIMAX_GROUP_ID", "ARK_API_KEY", "DEEPSEEK_API_KEY", "VOLC_SPEECH_API_KEY")
# 带签名的下载链接不能入库（只记录主机名）
SIGNED_URL = re.compile(r"(Signature|X-Amz-Signature|OSSAccessKeyId|Expires)=", re.IGNORECASE)
BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}")
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".csv"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tasks(out: Path) -> dict[str, dict[str, Any]]:
    """tasks.jsonl 按 task_id 去重（同一任务被 --resume 重复记录）。"""
    found: dict[str, dict[str, Any]] = {}
    path = out / "tasks.jsonl"
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("task_id"):
            found[str(rec["task_id"])] = rec
    return found


def _failures(out: Path) -> dict[str, int]:
    """各次运行 calls.jsonl 里的失败类型分布（status != ok 的 error 前缀）。"""
    counts: Counter[str] = Counter()
    for calls in sorted((out / "runs").glob("*/calls.jsonl")) if (out / "runs").is_dir() else []:
        for line in calls.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") != "ok":
                text = str(rec.get("error") or "unknown")
                counts[text.replace("VideoError: ", "").split(":", 1)[0]] += 1
    return dict(counts)


def _matches_remote(client: minimax.Client, url: str, sha256: str, attempts: int = 4) -> bool | None:
    """重新下载比对 sha256；代理隧道偶发中断，重试几次，仍失败返回 None（未能比对，不等于不一致）。"""
    for attempt in range(attempts):
        try:
            return hashlib.sha256(client.download(url)).hexdigest() == sha256
        except minimax.VideoError:
            time.sleep(2 * (attempt + 1))
    return None


def reconcile(out: Path, client: minimax.Client, *, redownload: bool = True) -> dict[str, Any]:
    """查询 tasks.jsonl 里每个任务（免费 GET），与 out/videos/<shot_id>.mp4 对账，写 out/ledger.json。"""
    rows: list[dict[str, Any]] = []
    last_ok: dict[str, str] = {}  # shot_id → 最后一个成功任务的 task_id（同一镜头重新提交过时，本地视频只对应它）
    states: dict[str, Any] = {}
    for task_id, rec in _tasks(out).items():
        states[task_id] = client.query(task_id, minimax.spec_of(rec["model"]).api)
        if states[task_id].status == "succeeded":
            last_ok[rec["shot_id"]] = task_id
    for task_id, rec in _tasks(out).items():
        model, resolution = rec["model"], rec["resolution"]
        api = minimax.spec_of(model).api
        state = states[task_id]
        seconds = state.usage.get("output_seconds") if state.status == "succeeded" else None  # v1（Hailuo）没有 usage：按请求时长查固定价
        cost = pricing.video_cost("minimax", model, resolution, rec["duration"], output_seconds=seconds) if state.status == "succeeded" else None
        video = out / "videos" / f"{rec['shot_id']}.mp4"
        row: dict[str, Any] = {
            "shot_id": rec["shot_id"], "task_id": task_id, "node_key": rec["node_key"], "model": model, "resolution": resolution,
            "requested_duration_s": rec["duration"], "status": state.status, "usage": state.usage,
            "latency_s": (state.updated_at - state.created_at) if state.created_at and state.updated_at else None,
            "cost_cny": cost.cny if cost else 0.0, "cost_basis": "usage × 文档单价（账单待核对）" if cost else "未成功任务不计费",
            "video_host": minimax.host_of(state.video_url) if state.video_url else None, "video": None,
        }
        if video.is_file() and last_ok.get(rec["shot_id"]) == task_id:
            info = media.probe(video)
            row["video"] = {
                "path": f"videos/{video.name}", "bytes": video.stat().st_size, "sha256": _sha256(video),
                "width": info.width, "height": info.height, "duration_s": info.duration_s, "fps": info.fps, "has_audio": info.has_audio,
            }
            if redownload and state.video_url:
                row["video"]["sha256_matches_remote"] = _matches_remote(client, state.video_url, row["video"]["sha256"])
        rows.append(row)
    done = [r for r in rows if r["status"] == "succeeded"]
    lat = sorted(r["latency_s"] for r in done if r["latency_s"] is not None)
    ledger = {
        "tasks": rows,
        "totals": {
            "tasks": len(rows), "succeeded": len(done), "output_seconds": sum(r["usage"].get("output_seconds", 0) for r in done),
            "cost_cny": round(sum(r["cost_cny"] for r in rows), 6),
            "latency_s": {"min": lat[0], "median": statistics.median(lat), "max": lat[-1]} if lat else None,
        },
        "failures_in_runs": _failures(out),
        "note": "latency_s = 供应商 updated_at − created_at（提交到完成）；费用依据 usage.output_seconds 与文档单价，未与账单核对。",
    }
    (out / LEDGER).write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ledger


def _secret_values() -> list[str]:
    return [v for v in (os.environ.get(k, "") for k in SECRET_ENV) if len(v) >= 8]


def _scan_secrets(root: Path) -> list[str]:
    errors, secrets = [], _secret_values()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(root)
        if any(s in text for s in secrets):
            errors.append(f"{rel} 含密钥环境变量的值")
        if BEARER.search(text):
            errors.append(f"{rel} 含 Bearer 令牌")
        if SIGNED_URL.search(text):
            errors.append(f"{rel} 含带签名的下载链接")
    return errors


def verify_ledger(directory: Path) -> list[str]:
    """核验一个带 ledger.json 的目录：文件存在、sha256 与 ffprobe 实际值一致、费用合计一致、对应关系已与远端比对。"""
    errors: list[str] = []
    ledger = json.loads((directory / LEDGER).read_text(encoding="utf-8"))
    total = 0.0
    seen_keys: set[str] = set()
    for row in ledger["tasks"]:
        label = f"{directory.name}/{row['shot_id']}"
        total += row["cost_cny"]
        if row["status"] != "succeeded":
            continue
        if row["node_key"] in seen_keys:
            errors.append(f"{label} 同一 node_key 有多个成功任务（重复提交，重复计费）")
        seen_keys.add(row["node_key"])
        meta = row["video"]
        if meta is None:
            errors.append(f"{label} 任务已成功但本地没有视频")
            continue
        path = directory / meta["path"]
        if not path.is_file():
            errors.append(f"{label} 缺少视频 {meta['path']}")
            continue
        if _sha256(path) != meta["sha256"]:
            errors.append(f"{label} sha256 与账本不一致")
        try:
            info = media.probe(path)
        except Exception as exc:  # noqa: BLE001 - 任何解析失败都是证据问题
            errors.append(f"{label} 媒体不可解析：{exc}")
            continue
        if (info.width, info.height) != (meta["width"], meta["height"]) or abs(info.duration_s - meta["duration_s"]) > 0.05:
            errors.append(f"{label} ffprobe 实际值与账本不一致")
        if meta.get("sha256_matches_remote") is False:
            errors.append(f"{label} 本地文件与供应商任务产物不是同一个（镜头对应可能错位）")
        elif meta.get("sha256_matches_remote") is None:
            errors.append(f"{label} 没有与供应商产物比对过 sha256（重跑 --reconcile）")
        if row["cost_cny"] <= 0:
            errors.append(f"{label} 成功任务没有费用")
    listed = {row["video"]["path"] for row in ledger["tasks"] if row.get("video")}
    for orphan in sorted((directory / "videos").glob("*.mp4")) if (directory / "videos").is_dir() else []:
        if f"videos/{orphan.name}" not in listed:
            errors.append(f"{directory.name}/{orphan.name} 不在账本里（没有对应的成功任务）")
    if abs(total - ledger["totals"]["cost_cny"]) > 1e-6:
        errors.append(f"{directory.name} 费用合计 {total:.6f} 与 totals {ledger['totals']['cost_cny']} 不一致")
    return errors


def du_bytes(root: Path) -> int:
    """证据体积：在 git 仓库里按“会入库的文件”（已跟踪 + 未忽略的新文件）计，忽略的本地产物（如抽帧）不算；否则按目录下全部文件。"""
    try:
        proc = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."], cwd=root,
                              capture_output=True, check=True, timeout=30)
        files = [root / name for name in proc.stdout.decode("utf-8").split("\0") if name]
    except (OSError, subprocess.SubprocessError):
        files = [p for p in root.rglob("*") if p.is_file()]
    return sum(f.stat().st_size for f in files if f.is_file())
