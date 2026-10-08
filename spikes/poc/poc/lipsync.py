"""P0-09 口型同步（MiniMax H3）：音频输入准备、A / B 请求构造与本地校验、dry-run、提交 / 下载 / 记录。

只用 MiniMax（用户指令 2026-10-08，不走 MediaKit / 火山口型）。路线字母是本步自己的编号，与 architecture §5.5.2 的“路线 A / B”不同：
- A 原生音画：首帧（P0-07 选定）+ 提示词写台词，H3 自己说话；
- B 参考音频：reference_image（P0-07 选定首帧）+ reference_audio（P0-05 方案 C 的配音），不能再带 first_frame（官方互斥规则）；
- C 回贴、对照、D 参考视频在后处理 / 条件步骤里做，不经本模块的提交。

官方文档（platform.minimax.cn/docs/api-reference/video-generation-v2-create，2026-10-08 读取）要点：
- content 项：{"type":"audio_url","audio_url":{"url":…},"role":"reference_audio"}；音频 WAV / MP3、单段 2–15 秒、≤3 段、合计 ≤15 秒、≤15 MB，
  地址可以是公网 URL / mm_file:// / data URI（data:audio/<小写格式>;base64,…）；参考视频 MP4 / MOV、≤50 MB、≤3 个、单段 2–15 秒；
- 出现任一 reference_* 时不能再出现 first_frame / last_frame；多模态参考的 ratio 可选（默认 adaptive）；
- 文档里没有 generate_audio 开关，台词写法只有示例“角色说话：…，音色参考音频1”，说明音频很可能是音色参考而不是驱动音频，需要冒烟判定；
- 查询 usage 有 total_seconds / input_seconds / output_seconds / input_image_count，单价文档未给，沿用 P0-08 登记的 H3 单价。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from poc import compose, images, media, minimax, pricing, runlog, script, tts, video, video_ledger

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "docs/reports/p0/P0-09"
MODEL = "MiniMax-H3"
ROUTES = ("A", "B")
# 有对白且有 H3 片段的 5 个镜头（sc02_sh03 / sc02_sh04 / sc03_sh03 因 D-013 没有片段）；冒烟镜头 sc01_sh04 排第一
SHOTS = ("ep01_sc01_sh04", "ep01_sc01_sh03", "ep01_sc03_sh01", "ep01_sc03_sh02", "ep01_sc01_sh05")
SMOKE_SHOT = SHOTS[0]

MIN_AUDIO_S, MAX_AUDIO_S = 2.0, 15.0
MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_AUDIO_CLIPS = 3
MAX_AUDIO_TOTAL_S = 15.0
AUDIO_RATE = 44100
QUOTA_CODE = video.QUOTA_CODE
AIGC_WATERMARK = False  # P0 中间产物不加 H3 原生水印；成片标识由 P0-11 的 compose 统一加（INV-08 只约束生产环境）


class LipsyncError(Exception):
    """本地校验不通过（不发请求）或输入缺失。"""


# ---- 音频输入 ----


@dataclass
class AudioInput:
    path: str  # 相对仓库根
    sha256: str
    dur_s: float
    speech: list[tuple[float, float]]  # 各句语音在音频内的起止（秒）
    lines: list[str]


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def audio_filter(lines: list[compose.LinePlan], total_s: float) -> tuple[list[str], str]:
    """每句 atrim + adelay 到镜头内位置，amix 合成一条轨，补足 / 截到 total_s。返回 (按 lines 顺序的输入文件, filter_complex)。"""
    inputs: list[str] = []
    parts: list[str] = []
    for i, ln in enumerate(lines):
        inputs.append(str(ROOT / ln.mp3))
        delay_ms = round(ln.start_s * 1000)
        parts.append(
            f"[{i}:a]atrim=start={ln.src_in_s:.4f}:end={ln.src_out_s:.4f},asetpts=PTS-STARTPTS,aresample={AUDIO_RATE},"
            f"aformat=channel_layouts=mono,adelay={delay_ms}:all=1[a{i}]"
        )
    labels = "".join(f"[a{i}]" for i in range(len(lines)))
    parts.append(f"{labels}amix=inputs={len(lines)}:normalize=0:duration=longest,apad=whole_dur={total_s:.4f},atrim=0:{total_s:.4f}[out]")
    return inputs, ";".join(parts)


def audio_length(lines: list[compose.LinePlan]) -> float:
    """音频应有的时长：最后一句结束 + TAIL_S，不足 2 秒补静音到 2 秒；超过 15 秒报错。"""
    if not lines:
        raise LipsyncError("镜头没有台词，不需要口型")
    end = max(ln.start_s + ln.dur_s for ln in lines) + compose.TAIL_S
    total = max(MIN_AUDIO_S, round(end, 4))
    if total > MAX_AUDIO_S:
        raise LipsyncError(f"音频 {total:.2f} 秒超过 {MAX_AUDIO_S:g} 秒")
    return total


def build_audio(lines: list[compose.LinePlan], out: Path) -> AudioInput:
    """把镜头内的配音（P0-05 方案 C 的逐句 mp3，按 ASR 起止剪辑）拼成一条 mp3，作为 reference_audio 的输入。"""
    total = audio_length(lines)
    inputs, graph = audio_filter(lines, total)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for path in inputs:
        cmd += ["-i", path]
    cmd += ["-filter_complex", graph, "-map", "[out]", "-ac", "1", "-ar", str(AUDIO_RATE), "-c:a", "libmp3lame", "-b:a", "128k", str(out)]
    media._run(cmd)
    data = out.read_bytes()
    return AudioInput(
        _rel(out), hashlib.sha256(data).hexdigest(), total,
        [(ln.speech_start_s, ln.speech_end_s) for ln in lines], [ln.line_id for ln in lines],
    )


# ---- 提示词与请求 ----


def _characters(ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in ir["characters"]}


def make_prompt(route: str, shot: dict[str, Any], lines: list[compose.LinePlan], chars: dict[str, dict[str, Any]]) -> str:
    """lipsync.v1：镜头动作（沿用 P0-08 的 make_prompt）+ 台词。A 写明声音特征；B 用官方示例的“音色参考音频1”。"""
    if route not in ROUTES:
        raise LipsyncError(f"未知路线 {route!r}")
    motion = video.make_prompt(shot)
    for cid, c in chars.items():  # P0-08 的镜头动作提示词里是角色 id，这里换成角色名
        motion = motion.replace(cid, c.get("name", cid))
    said = []
    for ln in lines:
        c = chars.get(ln.speaker, {})
        name = c.get("name", "角色")
        said.append((name, c.get("voice", ""), ln.text))
    if route == "A":
        speak = "；".join(f"{'先' if i == 0 and len(said) > 1 else '再' if i else ''}{n}（{v}）开口说话，台词：“{t}”" for i, (n, v, t) in enumerate(said))
        return f"{motion} {speak}。口型与台词同步，嘴部自然开合，只有人声，没有背景音乐。"
    speak = "；".join(f"{'先' if i == 0 and len(said) > 1 else '再' if i else ''}{n}说话：“{t}”" for i, (n, _v, t) in enumerate(said))
    return f"以图片1为角色参考。{motion} {speak}，音色参考音频1。口型与台词同步，嘴部自然开合，没有背景音乐。"


def duration_for(hint_s: float, audio_s: float) -> int:
    """整数秒：不短于 hint 与音频，H3 范围 4–15。"""
    d = math.ceil(max(hint_s, audio_s) - 1e-6)
    return min(15, max(4, d))


def audio_uri(data: bytes, fmt: str = "mp3") -> str:
    return f"data:audio/{fmt.lower()};base64,{base64.b64encode(data).decode('ascii')}"


@dataclass
class Job:
    route: str
    shot_id: str
    prompt: str
    duration: int
    resolution: str
    image_path: str
    image_sha256: str
    audio: AudioInput | None  # B 必填
    node_key: str
    aigc_watermark: bool = False
    model: str = MODEL

    @property
    def out_name(self) -> str:
        return f"{self.route}_{self.shot_id}"


def node_key(job_fields: list[Any]) -> str:
    return hashlib.sha256(json.dumps(job_fields, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_job(job: Job, image: bytes, audio: bytes | None) -> None:
    """本地校验（不发请求）：A 走 H3 首帧的全部限制；B 另校验 reference_* 与 first_frame 互斥（构造即互斥）、音频格式 / 大小 / 时长。"""
    try:
        minimax.validate_request(job.model, job.resolution, job.duration, job.prompt, image)
    except minimax.VideoError as exc:
        raise LipsyncError(str(exc)) from None
    if job.route == "A":
        return
    if job.audio is None or audio is None:
        raise LipsyncError("路线 B 需要 reference_audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise LipsyncError(f"音频 {len(audio)} 字节超过 {MAX_AUDIO_BYTES}")
    if not MIN_AUDIO_S <= job.audio.dur_s <= MAX_AUDIO_S or job.audio.dur_s > MAX_AUDIO_TOTAL_S:
        raise LipsyncError(f"音频时长 {job.audio.dur_s:.2f} 秒不在 {MIN_AUDIO_S:g}–{MAX_AUDIO_S:g} 秒内")
    if job.duration < job.audio.dur_s - 1e-6:
        raise LipsyncError(f"duration {job.duration} 秒短于音频 {job.audio.dur_s:.2f} 秒")


def body(job: Job, image: bytes, audio: bytes | None, *, preview: bool = False) -> dict[str, Any]:
    """请求体。preview=True 时媒体地址换成“大小 + sha256”占位，不含 base64。"""
    info = images.image_info(image)
    if preview:
        image_url = f"<data:image/{info.fmt};base64, {info.bytes} bytes, sha256={info.sha256}>"
        audio_url = f"<data:audio/mp3;base64, {len(audio or b'')} bytes, sha256={job.audio.sha256 if job.audio else '-'}>"
    else:
        image_url = minimax.data_uri(image, info.fmt)
        audio_url = audio_uri(audio or b"")
    first = job.route == "A"
    content: list[dict[str, Any]] = [{"type": "text", "text": job.prompt}]
    if first:
        content.append({"type": "image_url", "image_url": {"url": image_url}, "role": "first_frame"})
    else:
        content.append({"type": "image_url", "image_url": {"url": image_url}, "role": "reference_image"})
        content.append({"type": "audio_url", "audio_url": {"url": audio_url}, "role": "reference_audio"})
    return {
        "model": job.model, "content": content, "resolution": job.resolution, "duration": job.duration, "ratio": "adaptive",
        "aigc_watermark": job.aigc_watermark,
    }


# ---- 规划 ----


def plan(
    routes: tuple[str, ...] = ROUTES,
    shots: list[str] | None = None,
    resolution: str = "768P",
    out: Path = DEFAULT_OUT,
    ir_path: Path = script.SAMPLE_EP01,
    tts_dir: Path = compose.DEFAULT_TTS_DIR,
    keyframes: Path = compose.DEFAULT_KEYFRAMES,
    videos_dir: Path = compose.DEFAULT_VIDEOS,
    build: bool = True,
) -> list[Job]:
    """选 5 个对白镜头，按路线生成 Job。build=True 时在 out/audio 下生成 B 的音频输入（dry-run 也需要，用来校验时长）。"""
    bad = [r for r in routes if r not in ROUTES]
    if bad:
        raise LipsyncError(f"未知路线 {bad}（可选 {'、'.join(ROUTES)}）")
    chosen = list(shots) if shots else list(SHOTS)
    unknown = [s for s in chosen if s not in SHOTS]
    if unknown:
        raise LipsyncError(f"不在本步范围内的镜头：{unknown}（范围 {'、'.join(SHOTS)}）")
    doc, _ = tts.load_episode(ir_path)
    ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))
    chars = _characters(ir)
    shot_by_id = {s["shot_id"]: s for s in tts.shots_of(doc)}
    cplan = compose.plan(ir_path, videos_dir, tts_dir, keyframes)
    by_id = {s.shot_id: s for s in cplan.shots}
    jobs: list[Job] = []
    for sid in chosen:
        sp = by_id[sid]
        if not sp.lines:
            raise LipsyncError(f"{sid}：没有台词")
        if sp.source != "real":
            raise LipsyncError(f"{sid}：没有 H3 片段（D-013），不在本步范围")
        if sp.keyframe is None or sp.keyframe_sha256 is None:
            raise LipsyncError(f"{sid}：没有选定首帧")
        key_path = ROOT / sp.keyframe
        audio = build_audio(sp.lines, out / "audio" / f"{sid}.mp3") if build else None
        a_len = audio.dur_s if audio else audio_length(sp.lines)
        dur = duration_for(sp.hint_s, a_len)
        for route in routes:
            prompt = make_prompt(route, shot_by_id[sid], sp.lines, chars)
            use_audio = audio if route == "B" else None
            key = node_key([
                "lipsync.v1", route, MODEL, resolution, dur, AIGC_WATERMARK, prompt, sp.keyframe_sha256, use_audio.sha256 if use_audio else None,
            ])
            jobs.append(Job(route, sid, prompt, dur, resolution, _rel(key_path), sp.keyframe_sha256, use_audio, key, AIGC_WATERMARK))
    return jobs


def cost_of(job: Job, output_seconds: float | None = None) -> pricing.VideoCost:
    return pricing.video_cost("minimax", job.model, job.resolution, job.duration, output_seconds=output_seconds)


def _load_media(job: Job) -> tuple[bytes, bytes | None]:
    image = (ROOT / job.image_path).read_bytes()
    if hashlib_sha(image) != job.image_sha256:
        raise LipsyncError(f"{job.shot_id}：首帧 sha256 不匹配")
    audio = (ROOT / job.audio.path).read_bytes() if job.audio else None
    if job.audio and hashlib_sha(audio or b"") != job.audio.sha256:
        raise LipsyncError(f"{job.shot_id}：音频 sha256 不匹配")
    return image, audio


def hashlib_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preview(jobs: list[Job]) -> dict[str, Any]:
    rows = []
    for j in jobs:
        image, audio = _load_media(j)
        validate_job(j, image, audio)
        rows.append({
            "route": j.route, "shot_id": j.shot_id, "duration": j.duration, "resolution": j.resolution, "node_key": j.node_key,
            "audio": asdict(j.audio) if j.audio else None, "estimated_cost_cny": cost_of(j).cny, "guard_cost_cny": cost_of(j).guard_cny,
            "request": body(j, image, audio, preview=True),
        })
    return {"jobs": rows, "estimated_cost_cny": round(sum(r["estimated_cost_cny"] for r in rows), 6),
            "guard_cost_cny": round(sum(r["guard_cost_cny"] for r in rows), 6)}


# ---- 运行 ----


def _load_tasks(path: Path) -> dict[str, dict[str, Any]]:
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


def run_jobs(
    jobs: list[Job], out: Path, *, resume: bool = True, max_cost_cny: float = 60.0, poll_s: float = 5.0, timeout_s: float = 1200.0,
    client: minimax.Client | None = None, sleep=time.sleep,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    tasks_path = out / "tasks.jsonl"
    known = _load_tasks(tasks_path) if resume else {}  # 默认读 tasks.jsonl 去重：已提交的任务只查询，不重复提交 / 扣费；--force 才全部重提
    new_jobs = [j for j in jobs if not (j.node_key in known and not known[j.node_key].get("retry"))]
    estimated = sum(cost_of(j).guard_cny for j in new_jobs)
    if estimated > max_cost_cny + 1e-9:
        raise LipsyncError(f"费用保险：预计 ¥{estimated:.2f} 超过上限 ¥{max_cost_cny:g}")
    records: list[dict[str, Any]] = []
    cli = client or minimax.Client(env=dict(os.environ))
    with runlog.Run("lipsync", {"count": len(jobs), "routes": sorted({j.route for j in jobs})}, base_dir=out / "runs") as run:
        for job in jobs:
            target = out / "videos" / f"{job.out_name}.mp4"
            target.parent.mkdir(exist_ok=True)
            existing = known.get(job.node_key)
            if existing and existing.get("retry"):
                existing = None
            rec: dict[str, Any] = {"route": job.route, "shot_id": job.shot_id, "node_key": job.node_key, "model": job.model,
                                   "resolution": job.resolution, "duration": job.duration}
            try:
                image, audio = _load_media(job)
                validate_job(job, image, audio)
                with run.call("minimax", "video", job.model, job.node_key) as call:
                    if existing:
                        task_id = existing["task_id"]
                    else:
                        task_id = cli.submit_raw("POST", "/v2/video_generation", body(job, image, audio)).task_id
                        rec["task_id"] = task_id
                        with tasks_path.open("a", encoding="utf-8") as f:  # 提交后立即落盘（容器随时可能被回收）
                            f.write(json.dumps({**rec, "status": "submitted"}, ensure_ascii=False) + "\n")
                    rec["task_id"] = task_id
                    state = cli.query(task_id, "v2")
                    deadline = time.monotonic() + timeout_s
                    while not state.terminal and time.monotonic() < deadline:
                        sleep(poll_s)
                        state = cli.query(task_id, "v2")
                    if not state.terminal:
                        raise minimax.VideoError("timeout", f"任务 {task_id} 超时")
                    if state.status != "succeeded":
                        raise minimax.failure_of(state)
                    call.usage = state.usage
                    cost = cost_of(job, state.usage.get("output_seconds"))
                    call.cost_cny, call.cost_basis = (0.0, "free") if existing else (cost.cny, "estimate")
                    data = cli.download(cli.download_url(state, "v2"))
                    target.write_bytes(data)
                    info = media.probe(target)
                    rec.update(status="succeeded", usage=state.usage, bytes=len(data), media=asdict(info), cost_cny=cost.cny)
                    records.append(rec)
                    with tasks_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({**{k: rec[k] for k in ("route", "shot_id", "node_key", "task_id", "model", "resolution", "duration")},
                                            "status": "succeeded", "usage": state.usage}, ensure_ascii=False) + "\n")
            except Exception as exc:
                rec.update(status="error", error=minimax.scrub(str(exc))[:400])
                records.append(rec)
                if isinstance(exc, minimax.VideoError) and exc.kind == "task_failed" and rec.get("task_id") and not existing:
                    with tasks_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({**rec, "status": "failed", "retry": True}, ensure_ascii=False) + "\n")
                if video._abort(exc):
                    records.append({"status": "aborted", "error": f"账号级 / 额度错误，中止其余镜头：{minimax.scrub(str(exc))[:200]}"})
                    break
    summary_path = out / "run-summary.json"
    merged: dict[str, dict[str, Any]] = {}
    if summary_path.exists():  # 多次运行（冒烟、全量、补跑）累积：按 node_key 取最新一条
        for r in json.loads(summary_path.read_text(encoding="utf-8")).get("jobs", []):
            if r.get("node_key"):
                merged[r["node_key"]] = r
    for r in records:
        if r.get("node_key"):
            merged[r["node_key"]] = r
    jobs_all = list(merged.values()) + [r for r in records if not r.get("node_key")]
    summary = {"jobs": jobs_all, "count": len(jobs_all), "success": sum(r.get("status") == "succeeded" for r in jobs_all)}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"jobs": records, "count": len(records), "success": sum(r.get("status") == "succeeded" for r in records)}


# ---- 评审包与核验 ----


def build_review_pack(out: Path, seed: int = 20261008) -> dict[str, Any]:
    """评审包：每个镜头 × {对照, A, B, C}（有就放）随机编号成 review/rNN.mp4，答案表放在 analysis/review-answers.json（评分前别看）。

    对照 = P0-08 的无台词片段 + P0-05 配音按 P0-11 的排法（镜头内偏移）混成的音轨，等于 P0-11 成片里这个镜头现在的样子。
    """
    import random
    import shutil

    items: list[tuple[str, str, Path]] = []
    for sid in SHOTS:
        a_video = out / "videos" / f"A_{sid}.mp4"
        if not (a_video.exists() or (out / "videos" / f"B_{sid}.mp4").exists()):
            continue
        ctrl = out / "analysis" / "control" / f"{sid}.mp4"
        ctrl.parent.mkdir(parents=True, exist_ok=True)
        media._run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(compose.DEFAULT_VIDEOS / f"{sid}.mp4"), "-i", str(out / "audio" / f"{sid}.mp3"),
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", str(ctrl)])
        items.append((sid, "对照", ctrl))
        for route in ("A", "B", "C"):
            path = out / "videos" / f"{route}_{sid}.mp4"
            if path.exists():
                items.append((sid, route, path))
    rng = random.Random(seed)
    order = list(range(len(items)))
    rng.shuffle(order)
    review = out / "review"
    if review.exists():
        shutil.rmtree(review)
    review.mkdir(parents=True)
    answers = []
    for n, idx in enumerate(order, start=1):
        sid, route, path = items[idx]
        code = f"r{n:02d}"
        shutil.copyfile(path, review / f"{code}.mp4")
        answers.append({"code": code, "shot_id": sid, "route": route})
    answers.sort(key=lambda a: (a["shot_id"], a["route"]))
    (out / "analysis" / "review-answers.json").write_text(json.dumps({"seed": seed, "answers": answers}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"count": len(answers), "dir": str(review)}


def verify(out: Path, max_bytes: int | None = None) -> tuple[bool, list[str]]:
    """核验证据目录：成功任务与视频文件一一对应、同一 node_key 不重复成功、费用与 run-summary 一致、无密钥与带签名链接、体积上限。"""
    errors: list[str] = []
    tasks_path = out / "tasks.jsonl"
    summary_path = out / "run-summary.json"
    if not tasks_path.exists() or not summary_path.exists():
        return False, ["缺少 tasks.jsonl 或 run-summary.json"]
    succeeded: dict[str, dict[str, Any]] = {}
    for line in tasks_path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("status") == "succeeded":
            succeeded[rec["node_key"]] = rec
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    seen_tasks: dict[str, str] = {}
    for rec in summary["jobs"]:
        if rec.get("status") != "succeeded":
            continue
        label = f"{rec['route']}_{rec['shot_id']}"
        if rec["node_key"] not in succeeded:
            errors.append(f"{label} 在 run-summary 里成功，但 tasks.jsonl 没有成功记录")
        if rec.get("task_id") in seen_tasks:
            errors.append(f"{label} 与 {seen_tasks[rec['task_id']]} 是同一个任务")
        seen_tasks[rec.get("task_id")] = label
        video = out / "videos" / f"{label}.mp4"
        if not video.exists():
            errors.append(f"{label} 缺少视频文件")
        elif video.stat().st_size != rec.get("bytes"):
            errors.append(f"{label} 视频大小与记录不一致")
    for video in sorted((out / "videos").glob("[AB]_*.mp4")):
        if not any(f"{r['route']}_{r['shot_id']}" == video.stem and r.get("status") == "succeeded" for r in summary["jobs"]):
            errors.append(f"{video.name} 没有对应的成功任务")
    calls_total = 0.0
    for calls in sorted((out / "runs").glob("*/calls.jsonl")):
        for line in calls.read_text(encoding="utf-8").splitlines():
            calls_total += json.loads(line).get("cost_cny") or 0.0
    summary_total = sum(r.get("cost_cny") or 0.0 for r in summary["jobs"] if r.get("status") == "succeeded")
    if abs(calls_total - summary_total) > 1e-6:
        errors.append(f"费用不一致：calls.jsonl 合计 ¥{calls_total:.2f}，run-summary 合计 ¥{summary_total:.2f}")
    errors += video_ledger._scan_secrets(out)
    if max_bytes is not None and (size := video_ledger.du_bytes(out)) > max_bytes:
        errors.append(f"证据体积 {size} 字节超过上限 {max_bytes}")
    return not errors, errors


# ---- 命令行 ----


def _cmd(args: argparse.Namespace) -> int:
    routes = tuple(r.strip().upper() for r in args.routes.split(",") if r.strip())
    out = Path(args.export or DEFAULT_OUT)
    if args.verify:
        ok, errors = verify(out, args.max_bytes)
        print(json.dumps({"ok": ok, "errors": errors}, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if args.review:
        print(json.dumps(build_review_pack(out), ensure_ascii=False))
        return 0
    if args.analyze:
        from poc import lipsync_eval

        result = lipsync_eval.analyze(out, args.shots)
        print(lipsync_eval.render_table(result))
        return 0
    try:
        jobs = plan(routes, args.shots, args.resolution, out)
        if args.dry_run:
            print(json.dumps(preview(jobs), ensure_ascii=False, indent=2))
            return 0
        for j in jobs:  # 提交前全部再校验一遍
            validate_job(j, *_load_media(j))
        result = run_jobs(jobs, out, resume=not args.force, max_cost_cny=args.max_cost_cny)
    except (LipsyncError, compose.ComposeError, media.MediaError) as exc:
        print(f"错误：{exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] == result["count"] else 2


def add_parser(sub) -> None:
    p = sub.add_parser("lipsync", help="对白镜头口型同步（MiniMax H3，P0-09）")
    p.add_argument("--routes", default="A,B", help="路线，逗号分隔（A 原生音画 / B 参考音频）")
    p.add_argument("--shots", action="append", help="镜头 id（可多次）；默认 5 个对白镜头")
    p.add_argument("--resolution", default="768P")
    p.add_argument("--max-cost-cny", type=float, default=60.0)
    p.add_argument("--dry-run", action="store_true", help="离线列出任务与脱敏请求预览，不提交")
    p.add_argument("--resume", action="store_true", help="（默认行为，保留兼容）沿用 tasks.jsonl 里已提交的任务，只查询 / 下载")
    p.add_argument("--force", action="store_true", help="忽略 tasks.jsonl，全部重新提交（会重复扣费）")
    p.add_argument("--export", help="输出目录")
    p.add_argument("--review", action="store_true", help="生成盲评包：<输出目录>/review/rNN.mp4（随机编号），答案表在 analysis/review-answers.json")
    p.add_argument("--verify", action="store_true", help="核验证据目录（任务与视频对应、费用、密钥与签名链接扫描）")
    p.add_argument("--max-bytes", type=int, help="--verify 时的证据体积上限（字节）")
    p.add_argument("--analyze", action="store_true", help="对已有片段算 M1–M4（ASR + 嘴部开合），写 <输出目录>/analysis/")
    p.set_defaults(func=_cmd)
