"""P0-09 评测：对 lipsync 产出的片段（A / B）和对照（P0-08 无台词片段 + P0-05 配音）算 M1–M4，输出 analysis/metrics.json、曲线图与嘴部缩略条。

- M1：片段原声过豆包 ASR（poc/speech.py，约 ¥0.01 / 次），与台词的“同音字折叠 CER”；
- M2 / M3 / M4：poc/lipsync_metrics.py；语音窗口 A / B 取 ASR 的句起止，对照取 P0-05 的 ASR 起止经 compose 排进镜头后的窗口；
- 每个镜头单独成行；“比对照高 0.15”的 M3 第二半在汇总行里判。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from poc import audio, compose, config, lipsync, lipsync_metrics as lm, media, speech

CER_MAX = 0.15
R_MARGIN = 0.15
FACE_TAU = 0.45  # P0-07 的人脸相似度阈值
TEMPO_MIN, TEMPO_MAX = 0.8, 1.25  # 路线 C 配音变速的允许范围
IDENTITY_LABELS = ("f000", "f050", "f100")


def transcribe(path: Path, cache: Path) -> dict[str, Any]:
    """片段原声 → ASR（缓存到 cache；需要 VOLC_SPEECH_API_KEY）。"""
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    wav = cache.with_suffix(".wav")
    cache.parent.mkdir(parents=True, exist_ok=True)
    media._run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    try:
        rid = speech.asr_submit(wav.read_bytes(), fmt="wav")
        res = speech.poll_asr(rid, lambda r: speech.asr_query(r), interval=1.5, max_wait=120)
    finally:
        wav.unlink(missing_ok=True)
    out = {"text": res.text, "utterances": res.utterances, "silent": res.silent, "duration_ms": res.duration_ms}
    cache.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def cer_equiv(ref: str, hyp: str) -> float:
    r, h = audio.fold_equivalents(audio.normalize(ref)), audio.fold_equivalents(audio.normalize(hyp))
    return audio.cer(r, h).rate


def windows_from_asr(asr: dict[str, Any]) -> list[tuple[float, float]]:
    return [(u["start_ms"] / 1000, u["end_ms"] / 1000) for u in asr.get("utterances", []) if "start_ms" in u and "end_ms" in u]


def _clip(tracker: lm.MouthTracker, video: Path, env_source: Path, speech_windows: list[tuple[float, float]], key: str, out: Path) -> dict[str, Any]:
    opening, boxes = tracker.opening_series(video)
    env = lm.frame_envelope(lm.pcm(env_source), n_frames=len(opening))
    metrics = lm.evaluate(opening, env, speech_windows)
    lm.curve_image(lm.normalize_opening(opening), env, speech_windows, out / "analysis" / "curves" / f"{key}.png")
    lm.mouth_strip(tracker, video, boxes, speech_windows, out / "analysis" / "strips" / f"{key}.jpg")
    return metrics.as_dict() | {"n_frames": int(len(opening))}


# ---- 路线 C：保留 A 的画面，把 P0-05 配音按 H3 自己说话的语音区间贴回去 ----


def route_c_plan(lines: list[compose.LinePlan], windows: list[tuple[float, float]]) -> list[dict[str, Any]]:
    """每句配音 → (变速、延迟)。句数必须与 H3 原声的句数一致（ASR 的句），否则返回空（不可行）。

    tempo = 配音语音时长 ÷ H3 语音时长（> 1 加速，< 1 减速），必须在 [0.8, 1.25] 内，否则该句 feasible=False；
    delay = 该句语音在新轨上的起点（H3 语音起点）− 剪辑段内语音起点 ÷ tempo；delay < 0（配音要比轨道起点更早）也算不可行。
    """
    if len(lines) != len(windows):
        return []
    plan = []
    for ln, (ws, we) in zip(lines, windows):
        dub_s = max(ln.speech_end_s - ln.speech_start_s, 1e-3)
        tempo = dub_s / max(we - ws, 1e-3)
        lead = (ln.speech_start_s - ln.start_s) / tempo  # 剪辑段起点到语音起点的时间（变速后）
        delay = ws - lead
        plan.append({"line_id": ln.line_id, "tempo": round(tempo, 4), "delay_s": round(delay, 4), "feasible": TEMPO_MIN <= tempo <= TEMPO_MAX and delay >= 0,
                     "target_window": [round(ws, 4), round(we, 4)]})
    return plan


def build_route_c(video: Path, lines: list[compose.LinePlan], plan: list[dict[str, Any]], total_s: float, out: Path) -> Path:
    """A 的画面（流复制）+ 重排后的配音轨（aac）。

    每句先单独渲染成变速后的临时 wav（atempo 之后的时间戳会让同一条 filter 链里的 adelay 失效，实测音轨被截短），
    再用 adelay 排到 H3 语音起点并混合。
    """
    import tempfile

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        segs = []
        for i, (ln, p) in enumerate(zip(lines, plan)):
            seg = Path(td) / f"seg{i}.wav"
            media._run([
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(lipsync.ROOT / ln.mp3), "-af",
                f"atrim=start={ln.src_in_s:.4f}:end={ln.src_out_s:.4f},asetpts=PTS-STARTPTS,aresample={lipsync.AUDIO_RATE},"
                f"aformat=channel_layouts=mono,atempo={p['tempo']:.4f}", str(seg),
            ])
            segs.append(seg)
        parts = [f"[{i + 1}:a]adelay={max(0, round(p['delay_s'] * 1000))}:all=1[a{i}]" for i, p in enumerate(plan)]
        labels = "".join(f"[a{i}]" for i in range(len(plan)))
        parts.append(f"{labels}amix=inputs={len(plan)}:normalize=0:duration=longest,apad=whole_dur={total_s:.4f},atrim=0:{total_s:.4f}[out]")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
        for seg in segs:
            cmd += ["-i", str(seg)]
        cmd += ["-filter_complex", ";".join(parts), "-map", "0:v:0", "-map", "[out]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", str(out)]
        media._run(cmd)
    return out


# ---- M5：首帧 SSIM 与人脸相似度（沿用 P0-07 / P0-08 的 face --manifest 口径） ----


def identity(out: Path, routes: tuple[str, ...] = lipsync.ROUTES) -> dict[str, dict[str, Any]]:
    """每个 (路线, 镜头) 抽 f000 / f050 / f100，算首帧 SSIM（对 P0-07 选定首帧）和人脸 bank 相似度（对 P0-06 定妆基准库）。返回 {route_shot: {...}}。"""
    manifest_path = compose.DEFAULT_KEYFRAMES
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = {i["shot_id"]: i for i in manifest["images"] if i.get("selected")}
    result: dict[str, dict[str, Any]] = {}
    for route in routes:
        base = out / "analysis" / "m5" / route
        images, rows = [], {}
        for video in sorted((out / "videos").glob(f"{route}_*.mp4")):
            sid = video.stem[len(route) + 1:]
            item = items[sid]
            info = media.probe(video)
            frames = media.extract_frames(video, base / "frames" / sid, info, (0.0, 0.5, 1.0))
            rows[sid] = {"ssim_first": round(media.ssim(manifest_path.parent / item["file"], frames[0].path), 4)}
            for f in frames:
                images.append({"shot_id": sid, "characters": item["characters"], "scheme": f.label, "round": 1,
                               "file": f"frames/{sid}/{f.label}.jpg", "measurable": item.get("measurable")})
        if not images:
            continue
        mpath = base / "face-manifest.json"
        mpath.write_text(json.dumps({"references": manifest["references"], "images": images}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        refs: list[str] = []
        for cid, group in manifest["references"].items():  # 同一角色的第 1 张是锚点（主图），其余补进 bank
            for name in ("main", "neutral"):
                if name in group:
                    refs += ["--refs", f"{cid}={lipsync.ROOT / group[name]['path']}"]
        proc = subprocess.run([sys.executable, "-m", "poc", "face", "--manifest", str(mpath), *refs, "--json", str(base / "faces.json")],
                              capture_output=True, text=True, cwd=lipsync.ROOT / "spikes" / "poc")
        if proc.returncode != 0:
            raise RuntimeError(f"face 度量失败：{proc.stderr[-300:]}")
        faces = json.loads((base / "faces.json").read_text(encoding="utf-8"))
        sims: dict[str, dict[str, list[float]]] = {}
        for image in faces["images"]:
            for inst in image["instances"]:
                if inst.get("measurable") and inst.get("bank") is not None:
                    sims.setdefault(image["shot_id"], {}).setdefault(inst["char_id"], []).append(inst["bank"])
        for sid, row in rows.items():
            flat = [v for vs in sims.get(sid, {}).values() for v in vs]
            row["face_bank_min"] = round(min(flat), 4) if flat else None
            row["face_bank_mean"] = round(sum(flat) / len(flat), 4) if flat else None
            row["face_pass"] = None if not flat else bool(min(flat) >= FACE_TAU)
            result[f"{route}_{sid}"] = row
    return result


def dub_match(video: Path, dub: Path, rate: int = 16000, hop_s: float = 0.01) -> dict[str, float]:
    """片段原声里有没有“我给的配音”：输入配音与片段原声的包络（10 ms）在最佳延迟处的相关，以及该延迟（秒）。

    B 路线若音频只是音色参考，H3 会自己合成语音，相关应当很低；若原样带出，相关接近 1。
    """
    import numpy as np

    def env(x):
        hop = int(rate * hop_s)
        n = len(x) // hop
        return np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(1))

    ei, eo = env(lm.pcm(dub, rate)), env(lm.pcm(video, rate))
    best_r, best_lag = float("nan"), 0
    for lag in range(0, max(1, len(eo) - len(ei) + 1)):
        r = lm.pearson(ei, eo[lag: lag + len(ei)])
        if best_r != best_r or (r == r and r > best_r):
            best_r, best_lag = r, lag
    return {"corr": round(best_r, 4), "lag_s": round(best_lag * hop_s, 3)}


def analyze(out: Path = lipsync.DEFAULT_OUT, shots: list[str] | None = None, tracker: lm.MouthTracker | None = None) -> dict[str, Any]:
    config.load_env()
    cplan = compose.plan()
    by_id = {s.shot_id: s for s in cplan.shots}
    tracker = tracker or lm.MouthTracker()
    rows: list[dict[str, Any]] = []
    for sid in shots or list(lipsync.SHOTS):
        sp = by_id[sid]
        text = "".join(ln.text for ln in sp.lines)
        dub = out / "audio" / f"{sid}.mp3"
        ctrl_windows = [(ln.speech_start_s, ln.speech_end_s) for ln in sp.lines]
        have_a_b = [(r, out / "videos" / f"{r}_{sid}.mp4") for r in lipsync.ROUTES if (out / "videos" / f"{r}_{sid}.mp4").exists()]
        if not have_a_b:
            continue
        ctrl_video = compose.DEFAULT_VIDEOS / f"{sid}.mp4"
        row = {"route": "对照", "shot_id": sid, "text": text, "cer": 0.0, "speech_windows": ctrl_windows}
        row["metrics"] = _clip(tracker, ctrl_video, dub, ctrl_windows, f"ctrl_{sid}", out)
        rows.append(row)
        for route, video in have_a_b:
            asr = transcribe(video, out / "analysis" / "asr" / f"{route}_{sid}.json")
            windows = windows_from_asr(asr)
            row = {"route": route, "shot_id": sid, "text": text, "asr_text": asr["text"], "cer": round(cer_equiv(text, asr["text"]), 4),
                   "speech_windows": windows}
            if route == "B":
                row["dub_match"] = dub_match(video, dub)
            if not windows:
                row["metrics"] = lm.ClipMetrics(False, 0.0, notes=["ASR 没有句起止（片段里没有可识别的语音）"]).as_dict()
            else:
                row["metrics"] = _clip(tracker, video, video, windows, f"{route}_{sid}", out)
            rows.append(row)
            if route == "A" and windows:
                c_plan = route_c_plan(sp.lines, windows)
                crow = {"route": "C", "shot_id": sid, "text": text, "cer": 0.0, "speech_windows": windows, "c_plan": c_plan}
                if not c_plan or not all(p["feasible"] for p in c_plan):
                    crow["metrics"] = lm.ClipMetrics(False, 0.0, notes=["路线 C 不可行：句数不一致或变速超出 0.8–1.25"]).as_dict()
                else:
                    cvid = build_route_c(video, sp.lines, c_plan, media.probe(video).duration_s, out / "videos" / f"C_{sid}.mp4")
                    crow["metrics"] = _clip(tracker, cvid, cvid, windows, f"C_{sid}", out)
                rows.append(crow)
    ctrl_r = {r["shot_id"]: r["metrics"]["r0"] for r in rows if r["route"] == "对照"}
    for r in rows:
        if r["route"] == "对照":
            continue
        m = r["metrics"]
        c = ctrl_r.get(r["shot_id"])
        m["r0_minus_control"] = None if m.get("r0") is None or c is None else round(m["r0"] - c, 4)
        m["m3_full_pass"] = None if m.get("m3_pass") is None or m["r0_minus_control"] is None else bool(m["m3_pass"] and m["r0_minus_control"] >= R_MARGIN)
        r["m1_pass"] = r["cer"] <= CER_MAX
    ident = identity(out)
    for r in rows:
        r["m5"] = ident.get(f"{r['route']}_{r['shot_id']}")
    result = {"criteria": {"cer_max": CER_MAX, "onset_tol_s": lm.ONSET_TOL_S, "r_margin_over_control": R_MARGIN, "silence_ratio_max": lm.SILENCE_RATIO_MAX,
                           "measurable_min": lm.MEASURABLE_MIN, "surrogates": lm.SURROGATES, "face_tau": FACE_TAU, "tempo_range": [TEMPO_MIN, TEMPO_MAX]}, "rows": rows}
    path = out / "analysis" / "metrics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def render_table(result: dict[str, Any]) -> str:
    head = "路线\t镜头\tCER\t可测\t起声Δ(s)\tr0\t替身P95\t比对照\tbest lag\t静音比\tM1\tM2\tM3\tM4\tSSIM首\t脸最小"
    lines = [head]
    for r in result["rows"]:
        m = r["metrics"]
        mark = lambda v: "-" if v is None else ("✓" if v else "✗")  # noqa: E731
        lines.append("\t".join(str(x) for x in [
            r["route"], r["shot_id"][-9:], r["cer"], "是" if m["measurable"] else "否", m.get("onset_delta_s"), m.get("r0"), m.get("surrogate_p95"),
            m.get("r0_minus_control"), m.get("best_lag_s"), m.get("silence_ratio"), mark(r.get("m1_pass")), mark(m.get("m2_pass")),
            mark(m.get("m3_full_pass", m.get("m3_pass"))), mark(m.get("m4_pass")),
            (r.get("m5") or {}).get("ssim_first"), (r.get("m5") or {}).get("face_bank_min"),
        ]))
    return "\n".join(lines)
