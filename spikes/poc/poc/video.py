"""P0-08 图生视频编排：规划、dry-run、提交、查询、下载和离线验证。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from poc import config, media, minimax, pricing, runlog, seedance, video_ledger

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_IR = ROOT / "packages/drama-ir/examples/v0/ep01.json"
DEFAULT_MANIFEST = ROOT / "docs/reports/p0/P0-07/keyframe-r1/manifest.json"
DEFAULT_OUT = ROOT / "docs/reports/p0/P0-08"

ALIASES = {"h3": "MiniMax-H3", "h3max": "MiniMax-H3-Max", "hailuo23": "MiniMax-Hailuo-2.3", **{k: v for k, v in seedance.MODELS.items()}}

@dataclass
class Job:
    shot_id: str
    image_path: str
    image_sha256: str
    prompt: str
    duration: int
    model: str
    resolution: str
    node_key: str
    selected: bool = True

def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

def _shots(ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["shot_id"]: s for ep in ir.get("episodes", []) for sc in ep.get("scenes", []) for s in sc.get("shots", [])}

def make_prompt(shot: dict[str, Any]) -> str:
    chars = "；".join(f"{c.get('character_id','主体')}继续{c.get('action','移动')}，保持{c.get('emotion','自然')}表情" for c in shot.get("characters", []))
    action = chars or "环境与道具产生细微自然变化"
    movement = shot.get("framing", {}).get("movement", "镜头稳定")
    lighting = shot.get("lighting", "原有光线")
    return f"镜头以{movement}开始，{action}；光线保持{lighting}，运动自然、连续，主体保持身份与服装一致。"

def _node_key(model: str, resolution: str, duration: int, prompt: str, image_sha: str) -> str:
    raw = json.dumps([model, resolution, duration, prompt, image_sha], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()

def plan(manifest_path: Path = DEFAULT_MANIFEST, ir_path: Path = DEFAULT_IR, candidate: str = "h3", resolution: str = "768P", shots: list[str] | None = None) -> list[Job]:
    manifest = _load(manifest_path); ir = _load(ir_path); lookup = _shots(ir)
    model = ALIASES.get(candidate, candidate)
    if not model.startswith("MiniMax"):
        resolution = resolution.lower()
    jobs: list[Job] = []
    for item in manifest.get("images", []):
        if not item.get("selected"): continue
        sid = item["shot_id"]
        if shots and sid not in shots: continue
        path = manifest_path.parent / item["file"]
        if not path.is_file(): raise ValueError(f"首帧不存在：{path}")
        data = path.read_bytes(); sha = hashlib.sha256(data).hexdigest()
        if item.get("sha256") and item["sha256"] != sha: raise ValueError(f"首帧 sha256 不匹配：{sid}")
        shot = lookup[sid]; duration = int(shot["duration"]["hint_s"])
        if model == "MiniMax-H3-Max" and duration < 5: duration = 5
        if model == "MiniMax-Hailuo-2.3": duration = 6 if duration <= 6 else 10
        prompt = make_prompt(shot); key = _node_key(model, resolution, duration, prompt, sha)
        jobs.append(Job(sid, str(path), sha, prompt, duration, model, resolution, key))
    return jobs

def _cost(job: Job):
    if job.model.startswith("MiniMax"):
        return pricing.video_cost("minimax", job.model, job.resolution, job.duration)
    return pricing.video_cost("ark", job.model, job.resolution, job.duration, pixels=seedance.output_pixels(job.model, job.resolution))

def _client(model: str, env: dict[str, str]):
    return minimax.Client(env=env) if model.startswith("MiniMax") else seedance.Client(env=env)

def _api(model: str) -> str:
    return minimax.spec_of(model).api if model.startswith("MiniMax") else "ark"

QUOTA_CODE = "2056"  # MiniMax：已达到 Token Plan 用量上限（HTTP 429，账号级）


def _abort(exc: Exception) -> bool:
    """账号级、配置类错误和额度用尽：继续提交只会重复失败，中止整次运行。"""
    return isinstance(exc, minimax.VideoError) and (exc.fatal or (exc.kind == "rate_limit" and QUOTA_CODE in str(exc)))


def _billed(job: Job, state: minimax.TaskState):
    """成功任务的费用：MiniMax 以 usage.output_seconds 为准，其余按估算。"""
    if job.model.startswith("MiniMax"):
        return pricing.video_cost("minimax", job.model, job.resolution, job.duration, output_seconds=state.usage.get("output_seconds"))
    return _cost(job)


def _load_tasks(path: Path) -> dict[str, dict[str, Any]]:
    """tasks.jsonl 按 node_key 取最后一条（任务提交后立即写入；同一任务的状态更新追加新行）。"""
    found: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("node_key"):
                found[rec["node_key"]] = rec
    return found


def run_jobs(jobs: list[Job], out: Path, *, dry_run: bool = False, resume: bool = False, max_cost_cny: float = 100.0, poll_s: float = 3.0, timeout_s: float = 900.0) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True); tasks_path = out / "tasks.jsonl"; records = []
    known = _load_tasks(tasks_path) if resume else {}
    # 费用保险只算这次会新提交的任务：resume 沿用的已有任务只查询、不新花钱（瞬时失败的旧任务会重新提交，仍计入）
    new_jobs = [j for j in jobs if not (j.node_key in known and not known[j.node_key].get("retry"))]
    estimated = sum(_cost(j).guard_cny for j in new_jobs)
    if estimated > max_cost_cny + 1e-9: raise ValueError(f"费用保险：预计 ¥{estimated:.2f} 超过上限 ¥{max_cost_cny:g}")
    if dry_run:
        return {"jobs": [asdict(j) | {"image_path": str(j.image_path), "estimated_cost_cny": _cost(j).cny, "guard_cost_cny": _cost(j).guard_cny} for j in jobs], "estimated_cost_cny": round(sum(_cost(j).cny for j in jobs), 6), "guard_cost_cny": round(sum(_cost(j).guard_cny for j in jobs), 6)}
    with runlog.Run("video", {"candidate": jobs[0].model if jobs else None, "count": len(jobs)}, base_dir=out / "runs") as run:
        for job in jobs:
            target = out / "videos" / f"{job.shot_id}.mp4"; target.parent.mkdir(exist_ok=True)
            existing = known.get(job.node_key)
            if existing and existing.get("retry"):
                existing = None  # 上次是瞬时失败：重新提交
            client = _client(job.model, dict(os.environ))
            try:
                image = Path(job.image_path).read_bytes()
                req = minimax.VideoRequest(job.model, job.resolution, job.duration, job.prompt, image) if job.model.startswith("MiniMax") else seedance.VideoRequest(job.model, job.resolution.lower(), job.duration, job.prompt, image)
                with run.call("minimax" if job.model.startswith("MiniMax") else "ark", "video", job.model, job.node_key) as call:
                    task_id = existing["task_id"] if existing else client.submit(req).task_id
                    rec = {"shot_id": job.shot_id, "node_key": job.node_key, "task_id": task_id, "model": job.model, "resolution": job.resolution, "duration": job.duration}
                    if not existing:  # 新任务提交后立即落盘；resume 沿用的任务不再追加
                        with tasks_path.open("a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False)+"\n")
                    state = client.query(task_id, _api(job.model))
                    deadline=time.monotonic()+timeout_s
                    while state.status not in minimax.TERMINAL and time.monotonic()<deadline:
                        time.sleep(poll_s); state=client.query(task_id, _api(job.model))
                    if state.status not in minimax.TERMINAL: raise minimax.VideoError("timeout", f"任务 {task_id} 超时")
                    if state.status != "succeeded": raise minimax.failure_of(state)
                    # 任务成功即已计费：先记账，再下载（下载失败不影响费用记录，--resume 只重新取回）
                    call.usage = state.usage
                    if existing:
                        call.cost_cny, call.cost_basis = 0.0, "free"  # 沿用旧任务，费用已在首次运行计入（见 ledger.json）
                    else:
                        call.cost_cny, call.cost_basis = _billed(job, state).cny, "estimate"
                    body=client.download(client.download_url(state, _api(job.model))); target.write_bytes(body)
                    info=media.probe(target); rec.update(status=state.status, usage=state.usage, bytes=len(body), media=asdict(info))
                    records.append(rec)
            except Exception as exc:
                records.append({"shot_id":job.shot_id,"node_key":job.node_key,"status":"error","error":str(exc)})
                if isinstance(exc, minimax.VideoError) and exc.kind == "task_failed" and "rec" in locals() and not existing:
                    with tasks_path.open("a", encoding="utf-8") as f:  # 非审核类的终态失败：标记可重新提交（审核类、账号类不重试）
                        f.write(json.dumps({**rec, "status": "failed", "retry": True}, ensure_ascii=False)+"\n")
                if _abort(exc):
                    records.append({"status": "aborted", "error": f"账号级 / 额度错误，中止其余 {len(jobs) - len(records)} 个镜头：{exc}"})
                    break
    summary={"jobs":records,"count":len(records),"success":sum(r.get("status")=="succeeded" for r in records)}
    (out/"run-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    return summary

def verify(path: Path, max_bytes: int | None = None) -> tuple[bool, list[str]]:
    """离线核验证据目录：每个带 ledger.json 的子目录（含自身）对账；全目录扫密钥与带签名链接；可选体积上限。"""
    ledgers = sorted({p.parent for p in [path / video_ledger.LEDGER, *path.glob(f"*/{video_ledger.LEDGER}")] if p.is_file()})
    if not ledgers:
        return False, ["缺少 run-summary.json 或 ledger.json（先用 --reconcile 生成账本）"]
    errors: list[str] = []
    for directory in ledgers:
        errors += video_ledger.verify_ledger(directory)
    errors += video_ledger._scan_secrets(path)
    if max_bytes is not None and (size := video_ledger.du_bytes(path)) > max_bytes:
        errors.append(f"证据体积 {size} 字节超过上限 {max_bytes}")
    return not errors, errors

def _cmd(args: argparse.Namespace) -> int:
    if args.analyze:
        from poc import video_metrics
        out = Path(args.analyze); result = video_metrics.analyze(out, Path(args.manifest), out / "analysis")
        print(json.dumps({"count": result["count"], "out": str(out / "analysis")}, ensure_ascii=False)); return 0
    if args.reconcile:
        out = Path(args.reconcile); ledger = video_ledger.reconcile(out, minimax.Client(env=dict(os.environ)))
        print(json.dumps(ledger["totals"], ensure_ascii=False, indent=2)); return 0
    if args.verify:
        ok, errors=verify(Path(args.verify), args.max_bytes); print(json.dumps({"ok":ok,"errors":errors},ensure_ascii=False,indent=2)); return 0 if ok else 1
    jobs=plan(Path(args.manifest),Path(args.ir),args.candidate,args.resolution,args.shots)
    result=run_jobs(jobs,Path(args.export or DEFAULT_OUT),dry_run=args.dry_run,resume=args.resume,max_cost_cny=args.max_cost_cny)
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0

def add_parser(sub) -> None:
    p=sub.add_parser("video",help="关键帧 → 图生视频片段（P0-08）")
    p.add_argument("--candidate",choices=tuple(ALIASES),default="h3"); p.add_argument("--resolution",default="768P")
    p.add_argument("--shots",action="append"); p.add_argument("--manifest",default=str(DEFAULT_MANIFEST)); p.add_argument("--ir",default=str(DEFAULT_IR))
    p.add_argument("--max-cost-cny",type=float,default=100.0); p.add_argument("--dry-run",action="store_true"); p.add_argument("--resume",action="store_true"); p.add_argument("--export",help="输出目录"); p.add_argument("--verify",metavar="目录"); p.add_argument("--reconcile",metavar="目录",help="免费查询 tasks.jsonl 里的任务并与本地视频对账，写 <目录>/ledger.json（需 MINIMAX_API_KEY）"); p.add_argument("--max-bytes",type=int,help="--verify 时的证据体积上限（字节）"); p.add_argument("--analyze",metavar="目录",help="对目录下 videos/*.mp4 做 ffprobe / SSIM / 运动量 / 抽帧，写入 <目录>/analysis")
    p.set_defaults(func=_cmd)
