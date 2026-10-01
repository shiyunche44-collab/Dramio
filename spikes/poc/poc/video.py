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

def run_jobs(jobs: list[Job], out: Path, *, dry_run: bool = False, resume: bool = False, max_cost_cny: float = 100.0, poll_s: float = 3.0, timeout_s: float = 900.0) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True); tasks_path = out / "tasks.jsonl"; records = []
    estimated = sum(_cost(j).guard_cny for j in jobs)
    if estimated > max_cost_cny + 1e-9: raise ValueError(f"费用保险：预计 ¥{estimated:.2f} 超过上限 ¥{max_cost_cny:g}")
    if dry_run:
        return {"jobs": [asdict(j) | {"image_path": str(j.image_path), "estimated_cost_cny": _cost(j).cny, "guard_cost_cny": _cost(j).guard_cny} for j in jobs], "estimated_cost_cny": round(sum(_cost(j).cny for j in jobs), 6), "guard_cost_cny": round(estimated, 6)}
    with runlog.Run("video", {"candidate": jobs[0].model if jobs else None, "count": len(jobs)}, base_dir=out / "runs") as run:
        for job in jobs:
            target = out / "videos" / f"{job.shot_id}.mp4"; target.parent.mkdir(exist_ok=True)
            existing = None
            if resume and tasks_path.exists():
                for line in tasks_path.read_text().splitlines():
                    try:
                        x=json.loads(line)
                        if x.get("node_key")==job.node_key: existing=x
                    except json.JSONDecodeError: pass
            client = _client(job.model, dict(os.environ))
            try:
                image = Path(job.image_path).read_bytes()
                req = minimax.VideoRequest(job.model, job.resolution, job.duration, job.prompt, image) if job.model.startswith("MiniMax") else seedance.VideoRequest(job.model, job.resolution.lower(), job.duration, job.prompt, image)
                with run.call("minimax" if job.model.startswith("MiniMax") else "ark", "video", job.model, job.node_key) as call:
                    submitted = None if existing else client.submit(req)
                    task_id = existing.get("task_id") if existing else submitted.task_id
                    rec = {"shot_id": job.shot_id, "node_key": job.node_key, "task_id": task_id, "model": job.model, "resolution": job.resolution, "duration": job.duration}
                    with tasks_path.open("a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False)+"\n")
                    state = client.query(task_id, _api(job.model))
                    deadline=time.monotonic()+timeout_s
                    while state.status not in minimax.TERMINAL and time.monotonic()<deadline:
                        time.sleep(poll_s); state=client.query(task_id, _api(job.model))
                    if state.status not in minimax.TERMINAL: raise minimax.VideoError("timeout", f"任务 {task_id} 超时")
                    if state.status != "succeeded": raise minimax.failure_of(state)
                    body=client.download(client.download_url(state, _api(job.model))); target.write_bytes(body)
                    info=media.probe(target); rec.update(status=state.status, usage=state.usage, bytes=len(body), media=asdict(info))
                    call.usage=state.usage; call.cost_cny=_cost(job).cny; call.cost_basis="estimate"
                    records.append(rec)
            except Exception as exc:
                records.append({"shot_id":job.shot_id,"node_key":job.node_key,"status":"error","error":str(exc)})
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
