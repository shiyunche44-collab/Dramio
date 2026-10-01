"""P0-11 OTIO 时间线：从规划（compose.Plan）构建 OpenTimelineIO，渲染器只读这个 OTIO（ADR-0009），并导出交换格式。

轨道（顺序固定）：
- V1 画面：每镜头一个 clip（real：引用 H3 片段；placeholder：引用 P0-07 首帧，metadata `placeholder=true`、缓推参数）；
  真实片段短于目标时，后面接一个“冻结尾帧”clip（metadata `freeze_last_frame=true`）。
- A1 对白：每句一个 clip（引用逐句 mp3，source_range 是对原文件的裁剪，非破坏性），句间 Gap。
- A2 环境声：真实片段自带的 AAC（clip 总是存在，`enabled` 随 `--ambient` 档位；增益写在 metadata）。
- A3 BGM、A4 SFX：空轨（P0-10 未做；镜头的 `sfx` 标签作为 V1 clip 的 marker 记录）。
- S1 字幕、O1 标识：GeneratorReference（文字 / 角标写在 parameters 与 metadata，NLE 不一定识别，字幕另有 SRT）。

素材一律相对 OTIO 文件所在目录的路径（`target_url`）；导出 FCP7 XML 时换成绝对路径。
没有安装 opentimelineio 时 `require_otio()` 抛 OtioMissing（带安装提示），其余模块不 import 它。
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from typing import Any

from poc import subtitles
from poc.render import AMBIENT_GAIN_DB, BADGE_TEXT
from poc.compose import FPS, HEIGHT, ROOT, WIDTH, Plan

INSTALL_HINT = "缺少 opentimelineio：pip install -e '.[otio]'（OpenTimelineIO + OpenTimelineIO-Plugins，导出 FCP7 XML / EDL 要用后者）"
AMBIENT_MODES = ("off", "low", "duck")
TRACKS = ("V1 画面", "A1 对白", "A2 环境声", "A3 BGM", "A4 SFX", "S1 字幕", "O1 标识")


class OtioMissing(Exception):
    pass


def require_otio():
    try:
        import opentimelineio as otio
    except ImportError as exc:
        raise OtioMissing(INSTALL_HINT) from exc
    return otio


def _rt(otio, frames: int):
    return otio.opentime.RationalTime(frames, FPS)


def _range(otio, start_frames: int, dur_frames: int):
    return otio.opentime.TimeRange(_rt(otio, start_frames), _rt(otio, dur_frames))


def _sec_frames(seconds: float) -> int:
    return round(seconds * FPS)


def _url(path: str, base: Path, absolute: bool) -> str:
    """素材地址：绝对（file:// URI，已百分号编码）或相对 OTIO 目录（逐段百分号编码，保留 /）。"""
    full = (ROOT / path).resolve()
    return full.as_uri() if absolute else quote(os.path.relpath(full, base))


def _resolve(url: str, base: Path) -> Path:
    """_url 的逆：file:// 或相对地址 → 绝对路径（解码百分号）；统一用于所有轨道。"""
    parts = urlsplit(url)
    return Path(unquote(parts.path)) if parts.scheme == "file" else (base / unquote(url)).resolve()


def build(plan: Plan, base_dir: Path, ambient: str = "off", absolute: bool = False):
    """规划 → OTIO Timeline。base_dir 是 OTIO 文件所在目录（相对路径的基准）。"""
    otio = require_otio()
    if ambient not in AMBIENT_MODES:
        raise ValueError(f"ambient 只能是 {AMBIENT_MODES}")
    tl = otio.schema.Timeline(name="ep01", global_start_time=_rt(otio, 0))
    tl.metadata["dramio"] = {
        "generator": "poc compose", "fps": FPS, "size": [WIDTH, HEIGHT], "ambient": ambient, "ir_sha256": plan.ir_sha256,
        "note": "BGM / SFX 为空轨（P0-10 未做）；字幕与角标是 GeneratorReference，另有 subtitles.srt",
    }
    tracks = {}
    for name in TRACKS:
        kind = otio.schema.TrackKind.Audio if name[0] == "A" else otio.schema.TrackKind.Video
        tracks[name] = otio.schema.Track(name=name, kind=kind)
        tl.tracks.append(tracks[name])
    tracks["A3 BGM"].metadata["dramio"] = {"empty": True, "reason": "P0-10 未做"}
    tracks["A4 SFX"].metadata["dramio"] = {"empty": True, "reason": "P0-10 未做；镜头 sfx 标签见 V1 的 markers"}

    for shot in plan.shots:
        meta = {"shot_id": shot.shot_id, "source": shot.source, "placeholder": shot.placeholder, "target_frames": shot.target_frames, "hint_s": shot.hint_s}
        if shot.placeholder:
            ref = otio.schema.ExternalReference(target_url=_url(shot.keyframe, base_dir, absolute))
            ref.available_range = _range(otio, 0, shot.target_frames)
            clip = otio.schema.Clip(name=shot.shot_id, media_reference=ref, source_range=_range(otio, 0, shot.target_frames))
            clip.metadata["dramio"] = meta | {"sha256": shot.keyframe_sha256, "still_image": True, "zoom": {"from": 1.0, "to": 1.08}}
            tracks["V1 画面"].append(clip)
        else:
            native = min(shot.target_frames, max(1, int(shot.video_s * FPS + 1e-6)))
            ref = otio.schema.ExternalReference(target_url=_url(shot.video, base_dir, absolute))
            ref.available_range = _range(otio, 0, max(1, int(shot.video_s * FPS + 1e-6)))
            clip = otio.schema.Clip(name=shot.shot_id, media_reference=ref, source_range=_range(otio, 0, native))
            clip.metadata["dramio"] = meta | {"sha256": shot.video_sha256, "native_s": shot.video_s}
            tracks["V1 画面"].append(clip)
            if native < shot.target_frames:
                # FreezeFrame：保持 source_range 起点那一帧，持续整个 clip 时长（起点 = 片段最后一帧，所以 source_range 会超出 available_range；
                # 这是 OTIO 对“冻结尾帧”的标准写法，但 FCP7 XML / EDL 适配器不一定保留它，见报告）
                freeze = otio.schema.Clip(name=f"{shot.shot_id} 冻结尾帧", media_reference=ref.clone(), source_range=_range(otio, max(0, native - 1), shot.target_frames - native))
                freeze.metadata["dramio"] = {"shot_id": shot.shot_id, "freeze_last_frame": True, "frames": shot.target_frames - native}
                freeze.effects.append(otio.schema.FreezeFrame(name="freeze"))
                tracks["V1 画面"].append(freeze)
        for tag in shot.sfx:
            marker = otio.schema.Marker(name=tag, marked_range=_range(otio, 0, 1))
            marker.metadata["dramio"] = {"kind": "sfx_tag"}
            clip.markers.append(marker)

    # A1 对白（绝对时间排布，句间 Gap）
    cursor = 0
    for shot in plan.shots:
        for ln in shot.lines:
            start = shot.start_frames + _sec_frames(ln.start_s)
            dur = _sec_frames(ln.dur_s)
            if start < cursor:
                raise ValueError(f"{ln.line_id}：对白起点 {start} 帧早于上一句的终点 {cursor} 帧（台词重叠）")
            if start > cursor:
                tracks["A1 对白"].append(otio.schema.Gap(source_range=_range(otio, 0, start - cursor)))
            ref = otio.schema.ExternalReference(target_url=_url(ln.mp3, base_dir, absolute))
            ref.available_range = otio.opentime.TimeRange(otio.opentime.RationalTime(0, FPS), otio.opentime.RationalTime(ln.file_s * FPS, FPS))
            clip = otio.schema.Clip(name=ln.line_id, media_reference=ref, source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(ln.src_in_s * FPS, FPS), _rt(otio, dur)))
            clip.metadata["dramio"] = {
                "line_id": ln.line_id, "shot_id": shot.shot_id, "speaker": ln.speaker, "kind": ln.kind, "text": ln.text, "sha256": ln.mp3_sha256,
                "src_in_s": ln.src_in_s, "src_out_s": ln.src_out_s,
            }
            tracks["A1 对白"].append(clip)
            cursor = start + dur
    # A2 环境声（只有真实片段且自带音频）
    gain = AMBIENT_GAIN_DB[ambient]
    cursor = 0
    for shot in plan.shots:
        if shot.placeholder or not shot.video_has_audio:
            continue
        native = min(shot.target_frames, int(shot.video_s * FPS + 1e-6))
        if shot.start_frames > cursor:
            tracks["A2 环境声"].append(otio.schema.Gap(source_range=_range(otio, 0, shot.start_frames - cursor)))
        ref = otio.schema.ExternalReference(target_url=_url(shot.video, base_dir, absolute))
        ref.available_range = _range(otio, 0, max(1, int(shot.video_s * FPS + 1e-6)))
        clip = otio.schema.Clip(name=f"{shot.shot_id} 原声", media_reference=ref, source_range=_range(otio, 0, native))
        clip.enabled = gain is not None
        clip.metadata["dramio"] = {"shot_id": shot.shot_id, "gain_db": gain, "mode": ambient, "sha256": shot.video_sha256}
        tracks["A2 环境声"].append(clip)
        cursor = shot.start_frames + native
    # S1 字幕
    cursor = 0
    for cue in subtitles.cues(plan):
        start, end = _sec_frames(cue.start_s), _sec_frames(cue.end_s)
        if start > cursor:
            tracks["S1 字幕"].append(otio.schema.Gap(source_range=_range(otio, 0, start - cursor)))
        gen = otio.schema.GeneratorReference(generator_kind="subtitle", parameters={"text": cue.text})
        clip = otio.schema.Clip(name=cue.line_id, media_reference=gen, source_range=_range(otio, 0, end - start))
        clip.metadata["dramio"] = {"line_id": cue.line_id, "shot_id": cue.shot_id, "kind": cue.kind, "text": cue.text,
                                   "start_s": cue.start_s, "end_s": cue.end_s, "box": subtitles.box(cue)}
        tracks["S1 字幕"].append(clip)
        cursor = end
    # O1 标识
    gen = otio.schema.GeneratorReference(generator_kind="aigc_badge", parameters={"text": BADGE_TEXT})
    badge = otio.schema.Clip(name=BADGE_TEXT, media_reference=gen, source_range=_range(otio, 0, plan.total_frames))
    badge.metadata["dramio"] = {"text": BADGE_TEXT, "visible": "全片", "switchable": False}
    tracks["O1 标识"].append(badge)
    return tl


# ---- 读回（渲染器的唯一输入） ----


def _plain(value: Any) -> Any:
    """OTIO 的 AnyDictionary / AnyVector → 普通 dict / list（原对象随 Timeline 销毁后不可用）。"""
    if isinstance(value, (str, bytes, int, float, bool)) or value is None:
        return value
    if hasattr(value, "keys"):
        return {k: _plain(value[k]) for k in value.keys()}
    return [_plain(v) for v in value]


def _meta(item) -> dict[str, Any]:
    return _plain(item.metadata.get("dramio", {}))


def to_spec(tl, base_dir: Path) -> dict[str, Any]:
    """OTIO → 渲染器用的普通 dict（不 import 规划层）。路径相对 base_dir 解析为绝对路径。"""
    otio = require_otio()
    dm = _plain(tl.metadata.get("dramio", {}))
    by_name = {t.name: t for t in tl.tracks}
    shots: list[dict[str, Any]] = []
    t = 0
    for item in by_name["V1 画面"]:
        meta = _meta(item)
        dur = int(round(item.duration().value))
        if meta.get("freeze_last_frame"):
            shots[-1]["freeze_frames"] = int(meta["frames"])
            t += int(meta["frames"])
            continue
        path = _resolve(item.media_reference.target_url, base_dir)
        shots.append({
            "shot_id": meta["shot_id"], "source": meta["source"], "placeholder": bool(meta["placeholder"]), "media": str(path),
            "sha256": meta.get("sha256"), "start_frames": t, "native_frames": dur, "target_frames": int(meta["target_frames"]),
            "freeze_frames": 0, "zoom": meta.get("zoom"), "hint_s": meta.get("hint_s"),
        })
        t += dur
    lines, cursor = [], 0
    for item in by_name["A1 对白"]:
        dur = int(round(item.duration().value))
        if isinstance(item, otio.schema.Clip):
            meta = _meta(item)
            lines.append({**meta, "media": str(_resolve(item.media_reference.target_url, base_dir)), "start_frames": cursor,
                          "in_s": item.source_range.start_time.value / FPS, "dur_s": dur / FPS})
        cursor += dur
    ambient, cursor = [], 0
    for item in by_name["A2 环境声"]:
        dur = int(round(item.duration().value))
        if isinstance(item, otio.schema.Clip):
            meta = _meta(item)
            ambient.append({**meta, "media": str(_resolve(item.media_reference.target_url, base_dir)), "start_frames": cursor,
                            "dur_s": dur / FPS, "enabled": item.enabled})
        cursor += dur
    cues_ = [_meta(i) for i in by_name["S1 字幕"] if isinstance(i, otio.schema.Clip)]
    badge_item = by_name["O1 标识"][0]
    badge = {**_meta(badge_item), "frames": int(round(badge_item.duration().value))}
    return {"fps": dm.get("fps", FPS), "size": dm.get("size", [WIDTH, HEIGHT]), "ambient": dm.get("ambient", "off"), "ir_sha256": dm.get("ir_sha256"),
            "total_frames": int(round(tl.duration().value)), "shots": shots, "lines": lines, "ambient_clips": ambient, "cues": cues_, "badge": badge}


def exchange_view(tl):
    """交换用视图：只含 V1 画面、A1 对白、A2 环境声（字幕 / 角标 / 空轨在剪辑软件里不可靠，字幕走 SRT）。"""
    otio = require_otio()
    view = otio.schema.Timeline(name=tl.name + "（交换用）", global_start_time=tl.global_start_time)
    for track in tl.tracks:
        if track.name in ("V1 画面", "A1 对白", "A2 环境声"):
            view.tracks.append(copy.deepcopy(track))
    return view


def write(tl, path: Path) -> None:
    require_otio().adapters.write_to_file(tl, str(path), adapter_name="otio_json")


def read(path: Path):
    return require_otio().adapters.read_from_file(str(path), adapter_name="otio_json")


def export(tl, path: Path, adapter: str) -> None:
    require_otio().adapters.write_to_file(tl, str(path), adapter_name=adapter)
