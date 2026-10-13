"""P0-13 规模抽测的离线汇总：读每集 `run` 的 pipeline-manifest.json、calls.jsonl 与节点缓存，输出分阶段费用 / 耗时 / 返工统计，
并为 `face` 准备参考图与 keyframe manifest（带 characters）。不调用任何供应商。

一集可以由多个 run 目录组成（被工具超时打断后用同一缓存续跑）：calls 全部合并，manifest 取最后一个。
阶段费用取自节点缓存记录创建时的 cost_cny / elapsed_s（续跑命中的节点仍记原始成本），与 calls.jsonl 合计对账。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from poc import nodecache


def load_calls(dirs: list[Path]) -> list[dict[str, Any]]:
    out = []
    for d in dirs:
        for line in (d / "calls.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def episode_summary(name: str, dirs: list[Path], cache_root: Path) -> dict[str, Any]:
    cache = nodecache.NodeCache(cache_root)
    man = json.loads((dirs[-1] / "pipeline-manifest.json").read_text(encoding="utf-8"))
    calls = load_calls(dirs)
    calls_cost = round(sum(float(c.get("cost_cny") or 0.0) for c in calls), 6)
    by_stage: dict[str, dict[str, float]] = defaultdict(lambda: {"nodes": 0, "cost_cny": 0.0, "elapsed_s": 0.0})
    repairs: dict[str, int] = {}
    for n in man["nodes"]:
        if n["stage"].startswith("compose"):
            st = by_stage["compose"]
            st["nodes"] += 1
            st["elapsed_s"] += n["elapsed_s"]  # 合成无供应商费用；耗时只有本次 run 的未命中耗时
            continue
        rec = cache.get(n["stage"], n["key"])
        st = by_stage[n["stage"]]
        st["nodes"] += 1
        if rec is not None:
            st["cost_cny"] += rec.cost_cny
            st["elapsed_s"] += rec.elapsed_s
            if n["stage"] in ("script", "shots"):
                repairs[n["stage"]] = int(rec.meta.get("repairs", 0))
    errors = [c for c in calls if c.get("status") != "ok"]
    shots = man["plan"]["shots"]
    dlg = [s for s in shots if s["lines"]]
    sum_lines = sum(len(s["lines"]) for s in shots)
    return {
        "name": name,
        "runs": [d.name for d in dirs],
        "logline": man["logline"],
        "shots": len(shots),
        "lines": sum_lines,
        "characters": len({c for s in shots for c in s["characters"]}),
        "total_s": man["plan"]["total_s"],
        "dialogue_shots": len(dlg),
        "multi_char_dialogue_shots": sum(1 for s in dlg if len(set(s["characters"])) >= 2),
        "extended_shots": sum(1 for w in man["warnings"] if "台词需要" in w),
        "placeholders": len(man["placeholders"]),
        "degraded": man["degraded"],
        "warnings": man["warnings"],
        "cost_cny_calls": calls_cost,
        "cost_cny_manifest_last_run": man["cost_cny"],
        "stage": {k: {"nodes": int(v["nodes"]), "cost_cny": round(v["cost_cny"], 4), "elapsed_s": round(v["elapsed_s"], 1)} for k, v in by_stage.items()},
        "elapsed_s_runs": round(sum(json.loads((d / "pipeline-manifest.json").read_text(encoding="utf-8"))["elapsed_s"] for d in dirs if (d / "pipeline-manifest.json").is_file()), 1),
        "llm_repairs": repairs,
        "error_calls": len(errors),
        "error_calls_cost_cny": round(sum(float(c.get("cost_cny") or 0.0) for c in errors), 6),
        "error_kinds": sorted({str(c.get("error"))[:80] for c in errors}),
        "hits_last_run": man["hits"],
        "misses_last_run": man["misses"],
    }


def totals(eps: list[dict[str, Any]]) -> dict[str, Any]:
    def cost(e: dict[str, Any], stage: str) -> float:
        return e["stage"].get(stage, {}).get("cost_cny", 0.0)

    t: dict[str, Any] = {
        "episodes": len(eps), "shots": sum(e["shots"] for e in eps), "lines": sum(e["lines"] for e in eps),
        "total_s": round(sum(e["total_s"] for e in eps), 2), "cost_cny_calls": round(sum(e["cost_cny_calls"] for e in eps), 4),
        "elapsed_s_runs": round(sum(e["elapsed_s_runs"] for e in eps), 1), "error_calls": sum(e["error_calls"] for e in eps),
        "error_calls_cost_cny": round(sum(e["error_calls_cost_cny"] for e in eps), 4),
    }
    t["cost_per_shot_cny"] = round(t["cost_cny_calls"] / t["shots"], 4) if t["shots"] else None
    t["cost_per_minute_cny"] = round(t["cost_cny_calls"] / (t["total_s"] / 60), 2) if t["total_s"] else None
    t["elapsed_per_shot_s"] = round(t["elapsed_s_runs"] / t["shots"], 1) if t["shots"] else None
    t["stage_cost_cny"] = {s: round(sum(cost(e, s) for e in eps), 4) for s in sorted({s for e in eps for s in e["stage"]})}
    t["cost_mean_per_episode_cny"] = round(statistics.mean(e["cost_cny_calls"] for e in eps), 3) if eps else None
    return t


def prepare_face(name: str, run_dir: Path, cache_root: Path, out_dir: Path) -> dict[str, Any]:
    """为一集准备 `face` 的输入：从 CAS 取出定妆参考图（主图 + neutral），生成带 characters 的 keyframe manifest。"""
    cache = nodecache.NodeCache(cache_root)
    man = json.loads((run_dir / "pipeline-manifest.json").read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    refs: list[str] = []
    for n in man["nodes"]:
        if n["stage"] in ("costume_main", "costume_neutral"):
            rec = cache.get(n["stage"], n["key"])
            if rec is None:
                raise FileNotFoundError(f"缓存缺少 {n['stage']}/{n['id']}")
            p = out_dir / f"{n['id']}-{n['stage'].split('_')[1]}.jpg"
            p.write_bytes(cache.read(rec, "image"))
            refs.append(f"{n['id']}={p}")
    refs.sort(key=lambda r: (r.split("=")[0], "main" not in r))  # 每个角色的主图在前，作锚点
    chars = {s["shot_id"]: s["characters"] for s in man["plan"]["shots"]}
    kf = json.loads((run_dir / "work" / "keyframes" / "manifest.json").read_text(encoding="utf-8"))
    images = [{**e, "file": str((run_dir / "work" / "keyframes" / e["file"]).resolve()), "characters": chars[e["shot_id"]]} for e in kf["images"]]
    mpath = out_dir / "manifest.json"
    mpath.write_text(json.dumps({"images": images}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"name": name, "manifest": str(mpath), "refs": refs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m poc.scale_summary")
    ap.add_argument("--ep", action="append", required=True, metavar="名字=run目录[,run目录…]", help="一集；多个目录表示被打断后续跑，最后一个是完整的")
    ap.add_argument("--cache", required=True, help="节点缓存目录")
    ap.add_argument("--out", help="写出 JSON")
    ap.add_argument("--face-prep", metavar="目录", help="为每集准备 face 输入（参考图与 manifest）到该目录")
    args = ap.parse_args(argv)
    eps, preps = [], []
    for item in args.ep:
        name, _, dirs = item.partition("=")
        ds = [Path(x) for x in dirs.split(",") if x]
        eps.append(episode_summary(name, ds, Path(args.cache)))
        if args.face_prep:
            preps.append(prepare_face(name, ds[-1], Path(args.cache), Path(args.face_prep) / name))
    result = {"episodes": eps, "totals": totals(eps), "face_prep": preps}
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
