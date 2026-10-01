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
    result = {"source_manifest": str(manifest_path), "count": len(rows), "shots": rows}
    (out_dir / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
