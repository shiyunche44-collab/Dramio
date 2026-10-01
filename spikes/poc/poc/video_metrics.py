"""P0-08 视频度量：逐镜头 ffprobe / 首帧 SSIM / 运动量 / 抽帧缩略条，并生成给 `face --manifest` 用的伪 manifest。"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from poc import media

LABELS = ("f000", "f050", "f100")
FRACTIONS = (0.0, 0.5, 1.0)


def analyze(video_dir: Path, manifest_path: Path, out_dir: Path) -> dict[str, Any]:
    """对 video_dir/videos/*.mp4 逐个度量，写 out_dir/metrics.json、frames/、sheets/ 与 face-manifest.json。"""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = {i["shot_id"]: i for i in manifest["images"] if i.get("selected")}
    rows, face_images = [], []
    for video in sorted((video_dir / "videos").glob("*.mp4")):
        shot_id = video.stem
        item = items[shot_id]
        key_frame = manifest_path.parent / item["file"]
        info = media.probe(video)
        frames = media.extract_frames(video, out_dir / "frames" / shot_id, info, FRACTIONS)
        ssim_first = media.ssim(key_frame, frames[0].path)
        mot = media.motion(video)
        media.contact_sheet([key_frame] + [f.path for f in frames], out_dir / "sheets" / f"{shot_id}.jpg", tile_width=200, tile_height=356)
        rows.append({
            "shot_id": shot_id, "characters": item["characters"], "measurable": item.get("measurable"),
            "keyframe_sha256": item["sha256"], "video_bytes": video.stat().st_size, "media": asdict(info),
            "ssim_first": ssim_first, "motion": mot.as_dict(),
        })
        for f in frames:
            face_images.append({"shot_id": shot_id, "characters": item["characters"], "scheme": f.label, "round": 1,
                                "file": f"frames/{shot_id}/{f.label}.jpg", "measurable": item.get("measurable")})
    (out_dir / "face-manifest.json").write_text(json.dumps({"references": manifest["references"], "images": face_images}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_table(video_dir, out_dir)
    result = {"source_manifest": str(manifest_path), "count": len(rows), "shots": rows}
    (out_dir / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def write_table(video_dir: Path, out_dir: Path) -> None:
    """逐镜头表 table.md：需要 metrics.json、faces.json（face --manifest 的输出）与 ledger.json；缺哪个就不写。"""
    paths = [out_dir / "metrics.json", out_dir / "faces.json", video_dir / "ledger.json"]
    if not all(p.is_file() for p in paths):
        return
    metrics, faces, ledger = (json.loads(p.read_text(encoding="utf-8")) for p in paths)
    sims: dict[str, dict[str, list[float]]] = {}
    for image in faces["images"]:
        for inst in image["instances"]:
            if inst.get("measurable") and inst.get("bank") is not None:
                sims.setdefault(image["shot_id"], {}).setdefault(inst["char_id"], {})[image["scheme"]] = inst["bank"]
    tasks = {r["shot_id"]: r for r in ledger["tasks"]}
    lines = ["| 镜头 | 角色 | 实际分辨率 | 时长（秒） | 首帧 SSIM | 运动量均值 / P95 | 人脸相似度 首 / 中 / 末 | 最小 | 耗时（秒） | 费用（元） | 体积（KB） |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in metrics["shots"]:
        sid, md = s["shot_id"], s["media"]
        by_char = sims.get(sid, {})
        cells, lows = [], []
        for char, frames in sorted(by_char.items()):
            cells.append(f"{char.removeprefix('char_')} " + " / ".join(f"{frames[l]:.2f}" if l in frames else "—" for l in LABELS))
            lows.append(min(frames.values()))
        task = tasks.get(sid, {})
        lines.append(
            f"| {sid} | {'、'.join(s['characters']) or '空镜'} | {md['width']}×{md['height']} | {md['duration_s']:.2f} | {s['ssim_first']:.3f} | "
            f"{s['motion']['mean']:.2f} / {s['motion']['p95']:.2f} | {'<br>'.join(cells) or '不可度量'} | {f'{min(lows):.2f}' if lows else '—'} | "
            f"{task.get('latency_s', '—')} | {task.get('cost_cny', '—')} | {s['video_bytes'] // 1024} |"
        )
    (out_dir / "table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
