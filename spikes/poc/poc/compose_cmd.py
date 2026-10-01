"""`python3 -m poc compose`：规划 → OTIO → FFmpeg 渲染 → 导出交换格式 → 核验（P0-11）。命令行接线与证据导出；逻辑在 compose / otio_timeline / render / subtitles。

用法见 README `compose` 一节。要点：
- `--dry-run`：只规划，打印镜头表（来源 / hint / 目标时长 / 延长 / 台词窗口），不写文件、不建缓存目录，不需要 opentimelineio。
- 默认：渲染到 `docs/reports/p0/P0-11/`（final.mp4、timeline.otio、timeline.xml、timeline.edl、subtitles.srt / .ass、compose-manifest.json、audio/ 三档原声对比）。
  缓存在 `runs/compose-cache/`（不入库），补入真实片段后重跑只重算被替换的镜头与终混。
- `--verify <目录>`：离线核验证据（规格、元数据、字幕、响度、OTIO、体积、密钥）。
- 没有 opentimelineio 时退出码 2 并给出安装提示。没有 AIGC 标识的关闭开关（无参数、无环境变量）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from poc import compose, config, media, otio_timeline, render, subtitles, video_ledger

LUFS_TOLERANCE = 1.5
SIZE_LIMIT_BYTES = 28 * 1024 * 1024
FACES_P08 = compose.ROOT / "docs/reports/p0/P0-08/full-h3/analysis/faces.json"
FACES_P07 = compose.ROOT / "docs/reports/p0/P0-07/faces-r1.json"
SAMPLE_TIMES = (0.1, 0.25, 0.5, 0.75, 0.95)  # 抽帧位置（占总时长的比例）


# ---- 字幕与人脸框 ----


def _face_boxes(plan: compose.Plan, p08: dict[str, Any], p07: dict[str, Any]) -> dict[str, list[list[float]]]:
    """每个镜头所有已检测人脸框，换算到 1080×1920 成片坐标。real：视频抽帧（768×1344，缩放到高 1920 再居中裁 1080）；
    placeholder：P0-07 的 ref2 首帧（1152×2048，缩放到 1080 宽，缓推最大 1.08 倍按最坏情况外扩）。"""
    boxes: dict[str, list[list[float]]] = {s.shot_id: [] for s in plan.shots}
    real = {s.shot_id for s in plan.shots if not s.placeholder}
    for im in p08.get("images", []):
        if im["shot_id"] in real:
            k = compose.HEIGHT / 1344
            off = (768 * k - compose.WIDTH) / 2
            for inst in (i for i in im["instances"] if i.get("bbox")):
                x0, y0, x1, y1 = inst["bbox"]
                boxes[im["shot_id"]].append([x0 * k - off, y0 * k, x1 * k - off, y1 * k])
    for im in p07.get("images", []):
        if im["shot_id"] in boxes and im["shot_id"] not in real and im.get("scheme") == "ref2":
            k, z = compose.WIDTH / 1152, 1.08
            cx, cy = compose.WIDTH / 2, compose.HEIGHT / 2
            for inst in (i for i in im["instances"] if i.get("bbox")):
                x0, y0, x1, y1 = (v * k for v in inst["bbox"])
                boxes[im["shot_id"]].append([(x0 - cx) * z + cx, (y0 - cy) * z + cy, (x1 - cx) * z + cx, (y1 - cy) * z + cy])
    return boxes


def face_overlap_table(plan: compose.Plan, p08: dict[str, Any], p07: dict[str, Any]) -> list[dict[str, Any]]:
    """字幕估算框与人脸框的相交检查（几何估算，不是像素级）。"""
    boxes = _face_boxes(plan, p08, p07)
    rows = []
    cue_by_shot: dict[str, list[subtitles.Cue]] = {}
    for c in subtitles.cues(plan):
        cue_by_shot.setdefault(c.shot_id, []).append(c)
    for shot in plan.shots:
        cues_ = cue_by_shot.get(shot.shot_id, [])
        faces = boxes[shot.shot_id]
        hits = []
        for c in cues_:
            b = subtitles.box(c)
            for f in faces:
                w, h = min(b["x1"], f[2]) - max(b["x0"], f[0]), min(b["y1"], f[3]) - max(b["y0"], f[1])
                if w > 0 and h > 0:
                    hits.append({"line_id": c.line_id, "overlap_px2": round(w * h)})
        rows.append({"shot_id": shot.shot_id, "source": shot.source, "cues": len(cues_), "faces_measured": len(faces),
                     "max_face_bottom_px": round(max((f[3] for f in faces), default=0)), "intersects": bool(hits), "hits": hits})
    return rows


# ---- 渲染与证据 ----


ONSET_TOLERANCE_S = 0.2  # 对白起点与字幕起点的最大偏差：ASR 的 start_ms 精度与静音检测阈值所限（实测最大约 0.15 秒）


def onset_offsets(dialogue_audio: Path, cue_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用只有对白的音轨（off 档）做静音检测，比较每句实际起声时刻与字幕起点（静音结束 − cue 起点，秒；正 = 声音晚于字幕）。"""
    proc = media._run([media.FFMPEG, "-nostdin", "-hide_banner", "-i", str(dialogue_audio), "-af", "silencedetect=noise=-38dB:d=0.1", "-f", "null", "-"], timeout=300)
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", proc.stderr)]
    rows = []
    for c in cue_list:
        near = min(ends, key=lambda e: abs(e - c["start_s"])) if ends else None
        rows.append({"line_id": c["line_id"], "cue_start_s": c["start_s"], "onset_s": round(near, 3) if near is not None else None,
                     "offset_s": round(near - c["start_s"], 3) if near is not None else None})
    return rows


def _tool_versions() -> dict[str, str]:
    import subprocess
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    return {"ffmpeg": out}


def _load_faces(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def run_compose(args: argparse.Namespace, out=sys.stdout) -> int:
    ir = Path(args.ir)
    try:
        p = compose.plan(ir, Path(args.videos), Path(args.tts_dir), Path(args.keyframes))
    except compose.ComposeError as exc:
        print(f"规划失败：{exc}", file=out)
        return 1
    real = sum(1 for s in p.shots if not s.placeholder)
    if args.dry_run:
        print(compose.render_table(p), file=out)
        for w in p.warnings:
            print(f"提示：{w}", file=out)
        cap = p.total_s * 2.5e6 / 8 / 1e6
        print(f"预计成片体积上限约 {cap:.0f} MB（2.5 Mbps 码率上限）；real {real} / placeholder {len(p.shots) - real}", file=out)
        return 0
    try:
        otio_timeline.require_otio()
    except otio_timeline.OtioMissing as exc:
        print(str(exc), file=out)
        return 2
    dest = Path(args.export)
    dest.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    tl = otio_timeline.build(p, dest, args.ambient)
    otio_path = dest / "timeline.otio"
    otio_timeline.write(tl, otio_path)
    spec = otio_timeline.to_spec(otio_timeline.read(otio_path), dest)
    cue_list = subtitles.cues(p)
    ass_path = dest / "subtitles.ass"
    ass_path.write_text(subtitles.ass(cue_list), encoding="utf-8")
    (dest / "subtitles.srt").write_text(subtitles.srt(cue_list), encoding="utf-8")
    result = render.render(spec, dest, Path(args.cache_dir), ass_path, args.ambient, variants=("off", "low", "duck"))
    # 三档原声对比：每档的音频节点已缓存；导出为小体积 m4a 并度量响度
    audio_dir = dest / "audio"
    audio_dir.mkdir(exist_ok=True)
    variants = {}
    for node in (n for n in result.nodes if n.kind == "audio_mix"):
        m4a = audio_dir / f"ambient-{node.name}.m4a"
        media._run([media.FFMPEG, "-nostdin", "-v", "error", "-y", "-i", node.path, "-c:a", "aac", "-b:a", "96k", str(m4a)], timeout=300)
        loud = media.loudness(m4a)
        variants[node.name] = {"file": f"audio/{m4a.name}", "lufs": loud.integrated_lufs, "true_peak_dbtp": loud.true_peak_dbtp, "node_key": node.key, "hit": node.hit,
                               "premix_lufs": render.premix_loudness(spec, node.name, render.Cache(Path(args.cache_dir)), int(spec["fps"]))}
    # 对白与原声的相对响度：对白（off 档，归一化前）响度 − (H3 原声的平均响度 + 档位增益)；duck 档的侧链压低没有计入（只会更大）
    native = [media.loudness(compose.ROOT / sp.video).integrated_lufs for sp in p.shots if sp.video and sp.video_has_audio]
    native_mean = round(sum(x for x in native if x is not None) / max(1, len([x for x in native if x is not None])), 2) if native else None
    dialogue = variants.get("off", {}).get("premix_lufs")
    for mode, v in variants.items():
        gain = render.AMBIENT_GAIN_DB[mode]
        v["ambient_native_lufs_mean"] = native_mean
        v["dialogue_minus_ambient_db"] = round(dialogue - (native_mean + gain), 1) if gain is not None and dialogue is not None and native_mean is not None else None
    # 交换格式：交换用视图（V1 / A1 / A2），FCP7 XML 与 EDL，回读核对
    view = otio_timeline.exchange_view(tl)
    exchange = {}
    for adapter, name in (("fcp_xml", "timeline.xml"), ("cmx_3600", "timeline.edl")):
        try:
            otio_timeline.export(view, dest / name, adapter)
            back = otio_timeline.require_otio().adapters.read_from_file(str(dest / name), adapter_name=adapter)
            exchange[adapter] = {"file": name, "ok": True, "tracks": [(t.name, str(t.kind), len(t)) for t in back.tracks],
                                 "duration_frames": int(round(back.duration().value))}
        except Exception as exc:  # noqa: BLE001 - 适配器的任何失败都如实记录
            exchange[adapter] = {"file": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    final = result.final
    info, loud = media.probe(final), media.loudness(final)
    # 抽帧证据：成片 5 个位置
    frames_dir = dest / "frames"
    frames_dir.mkdir(exist_ok=True)
    stills = []
    for frac in SAMPLE_TIMES:
        t = p.total_s * frac
        jpg = frames_dir / f"t{int(frac * 100):03d}.jpg"
        media._run([media.FFMPEG, "-nostdin", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(final), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(jpg)], timeout=120)
        stills.append(jpg)
    media.contact_sheet(stills, dest / "sheet.jpg", tile_width=360, tile_height=640)
    for jpg in stills:
        jpg.unlink()
    frames_dir.rmdir()
    manifest = {
        "plan": p.as_dict(), "selection": compose.manifest_entries(p), "ambient": args.ambient, "nodes": result.as_dict(),
        "elapsed_s": round(time.monotonic() - started, 2), "tools": _tool_versions(), "audio_variants": variants, "exchange": exchange,
        "face_overlap": face_overlap_table(p, _load_faces(FACES_P08), _load_faces(FACES_P07)),
        "final": {"file": "final.mp4", "bytes": final.stat().st_size, "sha256": compose.sha256_file(final), "width": info.width, "height": info.height,
                  "fps": info.fps, "duration_s": info.duration_s, "video_codec": info.video_codec, "audio_codec": info.audio_codec,
                  "pix_fmt": info.pix_fmt, "lufs": loud.integrated_lufs, "true_peak_dbtp": loud.true_peak_dbtp},
        "cues": [{"line_id": c.line_id, "shot_id": c.shot_id, "start_s": c.start_s, "end_s": c.end_s} for c in cue_list],
        "cost_cny": 0.0,
    }
    manifest["dialogue_onsets"] = onset_offsets(dest / "audio" / "ambient-off.m4a", manifest["cues"])
    (dest / "compose-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    d = result.as_dict()
    print(f"完成：{final}（{final.stat().st_size / 1e6:.1f} MB，{info.duration_s:.2f} 秒，{loud.integrated_lufs} LUFS）；节点命中 {d['hits']} / 未命中 {d['misses']}；real {real} / placeholder {len(p.shots) - real}", file=out)
    return 0


# ---- 核验 ----


def _srt_cues(path: Path) -> list[tuple[float, float]]:
    out = []
    for m in re.finditer(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", path.read_text(encoding="utf-8")):
        a = [int(x) for x in m.groups()]
        out.append((a[0] * 3600 + a[1] * 60 + a[2] + a[3] / 1000, a[4] * 3600 + a[5] * 60 + a[6] + a[7] / 1000))
    return out


def _roi_ymax(video: Path, t: float, x: int, y: int, w: int, h: int) -> float:
    proc = media._run([media.FFMPEG, "-nostdin", "-nostats", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                       "-vf", f"crop={w}:{h}:{x}:{y},signalstats,metadata=print:key=lavfi.signalstats.YMAX:file=-", "-f", "null", "-"], timeout=60)
    m = re.search(r"YMAX=([0-9.]+)", proc.stdout)
    return float(m.group(1)) if m else 0.0


def verify(directory: Path, max_bytes: int = SIZE_LIMIT_BYTES) -> tuple[bool, list[str], list[str]]:
    """返回 (是否通过, 错误, 提示)。错误 → 退出码 1；提示只是需要人看的事项。"""
    errors: list[str] = []
    notes: list[str] = []
    mpath = directory / "compose-manifest.json"
    if not mpath.is_file():
        return False, ["缺少 compose-manifest.json"], notes
    man = json.loads(mpath.read_text(encoding="utf-8"))
    final = directory / "final.mp4"
    if not final.is_file():
        return False, ["缺少 final.mp4"], notes
    info = media.probe(final)
    if (info.width, info.height) != (compose.WIDTH, compose.HEIGHT):
        errors.append(f"分辨率 {info.width}×{info.height} 不是 1080×1920")
    if info.fps != compose.FPS:
        errors.append(f"帧率 {info.fps} 不是 {compose.FPS}")
    if (info.video_codec, info.audio_codec, info.pix_fmt) != ("h264", "aac", "yuv420p"):
        errors.append(f"编码 {info.video_codec}/{info.audio_codec}/{info.pix_fmt} 不是 h264/aac/yuv420p")
    audio = media.probe_audio(final)
    if audio is None or audio.duration_s is None or abs(audio.duration_s - info.duration_s) > 0.05:
        errors.append(f"音视频时长相差超过 50 ms（视频 {info.duration_s}，音频 {audio.duration_s if audio else None}）")
    total_s = man["plan"]["total_s"]
    if abs(info.duration_s - total_s) > 1 / compose.FPS + 1e-6:
        errors.append(f"成片时长 {info.duration_s} 与 OTIO / 规划时长 {total_s} 相差超过 1 帧")
    if compose.sha256_file(final) != man["final"]["sha256"]:
        errors.append("final.mp4 的 sha256 与 manifest 不一致")
    # AIGC 隐式标识 + 可见角标
    tag = media.format_tags(final).get("aigc")
    try:
        if json.loads(tag or "")["Label"] != "1":
            errors.append("AIGC 元数据 Label 不是 1")
    except (ValueError, KeyError, TypeError):
        errors.append("容器元数据里没有可解析的 AIGC 标签")
    x0, y0, w, h = 760, 50, 280, 90
    bright = [_roi_ymax(final, total_s * f, x0, y0, w, h) for f in SAMPLE_TIMES]
    if min(bright) < 235:
        errors.append(f"角标区域在部分采样帧里没有白色文字（YMAX {bright}）")
    # 字幕：cue 数 = 台词数，且落在各自镜头窗口内
    shots = {s["shot_id"]: s for s in man["plan"]["shots"]}
    n_lines = sum(len(s["lines"]) for s in shots.values())
    srt_cues = _srt_cues(directory / "subtitles.srt") if (directory / "subtitles.srt").is_file() else []
    if len(man["cues"]) != n_lines or len(srt_cues) != n_lines:
        errors.append(f"字幕 cue 数（manifest {len(man['cues'])}，srt {len(srt_cues)}）不等于台词数 {n_lines}")
    for c in man["cues"]:
        s = shots[c["shot_id"]]
        lo, hi = s["start_frames"] / compose.FPS, (s["start_frames"] + s["target_frames"]) / compose.FPS
        if not (lo - 1e-6 <= c["start_s"] < c["end_s"] <= hi + 1e-6):
            errors.append(f"字幕 {c['line_id']} 不在镜头 {c['shot_id']} 的窗口内")
    # 响度
    lufs, tp = man["final"]["lufs"], man["final"]["true_peak_dbtp"]
    live = media.loudness(final)
    if live.integrated_lufs is None or abs(live.integrated_lufs - render.LUFS_TARGET) > LUFS_TOLERANCE:
        errors.append(f"整体响度 {live.integrated_lufs} LUFS 偏离 {render.LUFS_TARGET} 超过 ±{LUFS_TOLERANCE}")
    if live.true_peak_dbtp is None or live.true_peak_dbtp > -1.0:
        errors.append(f"真峰值 {live.true_peak_dbtp} dBTP 高于 −1")
    if (lufs, tp) != (live.integrated_lufs, live.true_peak_dbtp):
        notes.append("manifest 里的响度与重新测量的不同（ffmpeg 版本差异？）")
    offsets = [abs(r["offset_s"]) for r in man.get("dialogue_onsets", []) if r["offset_s"] is not None]
    if len(offsets) != n_lines or max(offsets, default=1.0) > ONSET_TOLERANCE_S:
        errors.append(f"对白起声与字幕起点偏差超过 {ONSET_TOLERANCE_S} 秒或有句子没检测到（最大 {max(offsets, default=None)}）")
    # 选片与占位：manifest 与磁盘一致；占位镜头在 OTIO 里同样标记
    for sel in man["selection"]:
        if sel["source"] == "real":
            path = compose.ROOT / sel["video"]
            if not path.is_file() or compose.sha256_file(path) != sel["video_sha256"]:
                errors.append(f"{sel['shot_id']}：真实片段缺失或 sha256 与 manifest 不一致")
        elif sel["video"] is not None:
            errors.append(f"{sel['shot_id']}：占位镜头不应有视频片段")
    # OTIO
    try:
        otio = otio_timeline.require_otio()
        tl = otio_timeline.read(directory / "timeline.otio")
        tracks = {t.name: t for t in tl.tracks}
        v = [c for c in tracks["V1 画面"] if isinstance(c, otio.schema.Clip) and not c.metadata["dramio"].get("freeze_last_frame")]
        a = [c for c in tracks["A1 对白"] if isinstance(c, otio.schema.Clip)]
        if (len(v), len(a)) != (len(man["selection"]), n_lines):
            errors.append(f"OTIO 的画面 clip {len(v)}、对白 clip {len(a)} 与 manifest（{len(man['selection'])} / {n_lines}）不一致")
        flags = {c.metadata["dramio"]["shot_id"]: bool(c.metadata["dramio"]["placeholder"]) for c in v}
        if flags != {s["shot_id"]: s["placeholder"] for s in man["selection"]}:
            errors.append("OTIO 里的 placeholder 标记与 manifest 不一致")
        if int(round(tl.duration().value)) != int(round(total_s * compose.FPS)):
            errors.append("OTIO 总时长与规划不一致")
        text = otio.adapters.write_to_string(tl, adapter_name="otio_json")
        if otio.adapters.write_to_string(otio.adapters.read_from_string(text, adapter_name="otio_json"), adapter_name="otio_json") != text:
            errors.append("OTIO 往返读写不一致")
    except otio_timeline.OtioMissing:
        notes.append("没有安装 opentimelineio，跳过 OTIO 核验")
    ok_exchange = [k for k, v in man["exchange"].items() if v.get("ok")]
    if not ok_exchange:
        errors.append("FCP7 XML 与 EDL 都没有导出成功")
    for k in ok_exchange:
        frames_back = man["exchange"][k]["duration_frames"]
        if frames_back != int(round(total_s * compose.FPS)):
            errors.append(f"{k} 回读时长 {frames_back} 帧与规划不一致")
    # 字幕与人脸
    bad = [r["shot_id"] for r in man["face_overlap"] if r["intersects"]]
    if bad:
        notes.append(f"字幕估算框与人脸框相交的镜头：{', '.join(bad)}（按 D-011 接受现状；见报告表）")
    # 体积、费用、密钥
    size = video_ledger.du_bytes(directory)
    if size > max_bytes:
        errors.append(f"证据体积 {size} 字节超过上限 {max_bytes}")
    if man.get("cost_cny", 0) != 0 or list(directory.rglob("calls.jsonl")):
        errors.append("P0-11 不应有费用或调用记录")
    errors += video_ledger._scan_secrets(directory)
    return not errors, errors, notes


def run_verify(directory: Path, max_bytes: int, out=sys.stdout) -> int:
    ok, errors, notes = verify(directory, max_bytes)
    print(json.dumps({"ok": ok, "errors": errors, "notes": notes}, ensure_ascii=False, indent=2), file=out)
    return 0 if ok else 1


def _cmd(args: argparse.Namespace) -> int:
    if args.verify:
        return run_verify(Path(args.verify), args.max_bytes)
    return run_compose(args)


def add_parser(sub) -> None:
    p = sub.add_parser("compose", help="剪辑合成：OTIO + FFmpeg 渲染 1080×1920 成片（P0-11）")
    p.add_argument("--ir", default=str(compose.script.SAMPLE_EP01))
    p.add_argument("--videos", default=str(compose.DEFAULT_VIDEOS), help="P0-08 视频片段目录（<shot_id>.mp4）；缺的镜头用首帧占位")
    p.add_argument("--tts-dir", default=str(compose.DEFAULT_TTS_DIR), help="P0-05 逐句配音目录")
    p.add_argument("--keyframes", default=str(compose.DEFAULT_KEYFRAMES), help="P0-07 首帧 manifest（selected）")
    p.add_argument("--ambient", choices=otio_timeline.AMBIENT_MODES, default="off", help="H3 片段原声：off / low / duck（对白侧链压低）")
    p.add_argument("--export", default=str(compose.DEFAULT_OUT), help="输出目录")
    p.add_argument("--cache-dir", default=str(config.PROJECT_DIR / "runs" / "compose-cache"))
    p.add_argument("--dry-run", action="store_true", help="只规划并打印镜头表，不写文件")
    p.add_argument("--verify", metavar="目录", help="离线核验证据目录")
    p.add_argument("--max-bytes", type=int, default=SIZE_LIMIT_BYTES, help="--verify 的证据体积上限（字节）")
    p.set_defaults(func=_cmd)
