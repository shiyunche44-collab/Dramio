"""run 子命令：一句话梗概 → 成片（P0-12）。引擎见 poc/pipeline.py。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from poc import compose, config, media, nodekey, pipeline, providers, render, runlog

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
DEFAULT_CACHE = Path("pipeline-cache")


def parse_reroll(items: list[str] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items or []:
        stage = it.split(":", 1)[0]
        if stage not in ("script", "shots", "costume", "tts", "keyframe"):
            raise ValueError(f"--reroll 的阶段必须是 script / shots / costume / tts / keyframe：{it!r}")
        out[it] = out.get(it, 0) + 1
    return out


def verify(directory: Path, out=sys.stdout) -> int:
    """离线复核一次运行：manifest 与成片规格、AIGC 标识、字幕条数、费用与 calls.jsonl、无密钥。"""
    errors: list[str] = []
    mpath = directory / "pipeline-manifest.json"
    if not mpath.is_file():
        print("缺少 pipeline-manifest.json", file=out)
        return EXIT_FAIL
    man = json.loads(mpath.read_text(encoding="utf-8"))
    final = directory / "final.mp4"
    if not final.is_file():
        errors.append("缺少 final.mp4")
    else:
        info = media.probe(final)
        if (info.width, info.height) != (compose.WIDTH, compose.HEIGHT):
            errors.append(f"分辨率 {info.width}×{info.height} 不是 1080×1920")
        if info.fps != compose.FPS:
            errors.append(f"帧率 {info.fps} 不是 {compose.FPS}")
        if (info.video_codec, info.audio_codec, info.pix_fmt) != ("h264", "aac", "yuv420p"):
            errors.append(f"编码 {info.video_codec}/{info.audio_codec}/{info.pix_fmt} 不是 h264/aac/yuv420p")
        audio = media.probe_audio(final)
        if audio is None or audio.duration_s is None or abs(audio.duration_s - info.duration_s) > 0.05:
            errors.append("音视频时长相差超过 50 ms")
        if abs(info.duration_s - man["plan"]["total_s"]) > 1 / compose.FPS + 1e-6:
            errors.append("成片时长与规划时长相差超过 1 帧")
        if nodekey.sha256_file(final) != man["final"]["sha256"]:
            errors.append("final.mp4 的 sha256 与 manifest 不一致")
        try:
            meta = json.loads(media.format_tags(final).get("aigc") or "")
            if meta["Label"] != "1" or not re.fullmatch(r"[0-9a-f]{32}", meta["ProduceID"]):
                errors.append("AIGC 元数据的 Label / ProduceID 不符合约定")
        except (ValueError, KeyError, TypeError):
            errors.append("容器元数据里没有可解析的 AIGC 标签")
    n_lines = sum(len(s["lines"]) for s in man["plan"]["shots"])
    srt = directory / "subtitles.srt"
    n_srt = len([b for b in srt.read_text(encoding="utf-8").split("\n\n") if b.strip()]) if srt.is_file() else -1
    if len(man["cues"]) != n_lines or n_srt != n_lines:
        errors.append(f"字幕 cue 数（manifest {len(man['cues'])}，srt {n_srt}）不等于台词数 {n_lines}")
    calls = directory / "calls.jsonl"
    if calls.is_file():
        total = sum(float(json.loads(x).get("cost_cny") or 0.0) for x in calls.read_text(encoding="utf-8").splitlines() if x.strip())
        if abs(total - man["cost_cny"]) > 1e-6:
            errors.append(f"manifest 费用 {man['cost_cny']} 与 calls.jsonl 合计 {total:.6f} 不一致")
    else:
        errors.append("缺少 calls.jsonl")
    if man["options"].get("require_real_video") and man.get("placeholders"):
        errors.append(f"严格模式（--require-real-video / --video h3）下仍有 {len(man['placeholders'])} 个占位镜头")
    if not all(set(n) >= {"stage", "key", "hit"} for n in man["nodes"]):
        errors.append("manifest 的节点缺少 stage / key / hit")
    secrets = providers.secret_values(os.environ)
    for f in directory.glob("*.json*"):
        text = f.read_text(encoding="utf-8", errors="replace")
        if any(s and s in text for s in secrets):
            errors.append(f"{f.name} 里含有密钥")
    placeholders = len(man.get("placeholders", []))
    for e in errors:
        print(f"失败：{e}", file=out)
    if not errors:
        print(f"核验通过：{final}（{man['final']['duration_s']} 秒，节点命中 {man['hits']} / 未命中 {man['misses']}，占位镜头 {placeholders}，费用 ¥{man['cost_cny']}）", file=out)
    return EXIT_FAIL if errors else EXIT_OK


def _cmd(args: argparse.Namespace, out=sys.stdout) -> int:
    if args.verify:
        return verify(Path(args.verify), out)
    if not args.logline:
        args.parser.error("需要 --logline（或用 --verify 复核已有运行）")
    try:
        reroll = parse_reroll(args.reroll)
    except ValueError as exc:
        args.parser.error(str(exc))
    opts = pipeline.Options(
        logline=args.logline, cache_dir=Path(args.cache_dir), base_dir=Path(args.out_dir) if args.out_dir else None,
        video=args.video, require_real_video=args.require_real_video, max_cost_cny=args.max_cost_cny, reroll=reroll,
    )
    pl = pipeline.Pipeline(opts)
    if args.dry_run:
        print(json.dumps(pl.dry_run(), ensure_ascii=False, indent=2), file=out)
        return EXIT_OK
    try:
        manifest = pl.run_all()
    except (pipeline.PipelineError, render.RenderError, media.MediaError, compose.ComposeError) as exc:
        print(config.redact(f"失败：{type(exc).__name__}: {exc}", providers.secret_values(os.environ)), file=out)
        return EXIT_FAIL
    run_dir = pl.run.dir
    print(f"完成：{run_dir / 'final.mp4'}（{manifest['final']['duration_s']} 秒）；节点命中 {manifest['hits']} / 未命中 {manifest['misses']}；"
          f"费用 ¥{manifest['cost_cny']}；占位镜头 {len(manifest['placeholders'])}；耗时 {manifest['elapsed_s']} 秒", file=out)
    for d in manifest["degraded"]:
        print(f"降级：{d.get('stage')}/{d.get('shot_id', '')}：{d['reason']}", file=out)
    print(f"输出：{run_dir}", file=out)
    if manifest["strict_failed"]:
        print("失败：严格模式（--require-real-video / --video h3）下仍有占位镜头", file=out)
        return EXIT_FAIL
    return EXIT_OK


def add_parser(sub) -> None:
    p = sub.add_parser("run", help="一句话梗概 → 成片，全自动（P0-12）")
    p.add_argument("--logline", help="一句话梗概")
    p.add_argument("--out-dir", help="运行目录的父目录（默认 runs/）")
    p.add_argument("--cache-dir", default=str(runlog.runs_dir() / "pipeline-cache"), help="节点缓存目录（默认 runs/pipeline-cache，不入库）")
    p.add_argument("--video", choices=("auto", "h3", "placeholder"), default="auto",
                   help="auto：H3 768P，额度用尽时占位降级；h3：同 auto 但有占位即失败；placeholder：不调视频接口，全部占位")
    p.add_argument("--require-real-video", action="store_true", help="有占位镜头时退出码为 1")
    p.add_argument("--max-cost-cny", type=float, default=50.0, help="整次运行累计估算费用上限（元）")
    p.add_argument("--reroll", action="append", metavar="阶段[:id]", help="让该阶段（或该节点）的采样序号加 1 重新生成，可重复")
    p.add_argument("--dry-run", action="store_true", help="离线预估，不需要密钥、不发请求")
    p.add_argument("--verify", metavar="运行目录", help="离线复核一次运行的成片与 manifest")
    p.set_defaults(func=_cmd, parser=p)
