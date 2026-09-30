"""把 DramaIR 文档渲染成可读的 Markdown 剧本摘要，便于人工审阅。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from dramio_drama_ir.checks import CHARS_PER_SECOND, speech_seconds

SHOT_SIZES = {"ECU": "大特写", "CU": "特写", "MCU": "近景", "MS": "中景", "MLS": "中远景", "LS": "远景", "ELS": "大远景"}
ANGLES = {"eye_level": "平视", "high": "俯拍", "low": "仰拍", "overhead": "顶拍", "dutch": "斜角"}
MOVEMENTS = {"static": "固定", "push_in": "推", "pull_out": "拉", "pan": "摇", "tilt": "俯仰", "tracking": "跟", "handheld": "手持"}
KINDS = {"dialogue": "", "voiceover": "（画外）"}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(doc: dict[str, Any]) -> str:
    series = doc["series"]
    names = {c["id"]: c["name"] for c in doc["characters"]}
    out: list[str] = [
        f"# {series['title']}",
        "",
        f"> 由 `python3 -m dramio_drama_ir render` 生成，请勿手改。DramaIR {doc['ir_version']}。",
        "",
        f"- **梗概**：{series['logline']}",
        f"- **类型**：{series['genre']}",
        f"- **视觉风格**：{series['visual_style']}",
        f"- **画幅 / 语言**：{series['aspect_ratio']} / {series['language']}",
        "",
        "## 角色",
        "",
        "| id | 姓名 | 性别 / 年龄 | 定位 | 小传 | 外貌 | 服装 | 声音 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in doc["characters"]:
        cells = [c["id"], c["name"], f"{c['gender']} / {c['age']}", c["role"], c["bio"], c["appearance"], c["costume"], c["voice"]]
        out.append("| " + " | ".join(_cell(str(x)) for x in cells) + " |")

    for ep in doc["episodes"]:
        out += ["", f"## 第 {ep['number']} 集 {ep['title']}（`{ep['id']}`）", "", ep["synopsis"]]
        shots = lines = 0
        total = speech = 0.0
        per_speaker: Counter[str] = Counter()
        n = 0
        for sc in ep["scenes"]:
            st = sc["setting"]
            out += [
                "",
                f"### {sc['scene_id']}　{st['int_ext']}. {st['location']} · {st['time_of_day']} · 情绪 {sc['mood']}",
                "",
                f"{st['description']}。{sc['summary']}。",
                "",
                "| # | 时长 | 景别 / 角度 / 运镜 | 画面 | 台词 | 音效 |",
                "|---|---|---|---|---|---|",
            ]
            for shot in sc["shots"]:
                n += 1
                f = shot["framing"]
                framing = f"{SHOT_SIZES[f['shot_size']]} / {ANGLES[f['angle']]} / {MOVEMENTS[f['movement']]}"
                cast = "；".join(f"{names[c['character_id']]}：{c['action']}（{c['emotion']}）" for c in shot["characters"])
                picture = shot["description"] + (f"<br>{cast}" if cast else "<br>（空镜）")
                said = "<br>".join(
                    f"**{names[l['speaker']]}**{KINDS[l['kind']]}：{l['text']}（{l['delivery']['emotion']}）"
                    for l in shot["dialogue"]
                )
                hint = shot["duration"]["hint_s"]
                row = [str(n), f"{hint:g}s", framing, picture, said, "、".join(shot["sfx"])]
                out.append("| " + " | ".join(_cell(x) for x in row) + " |")
                shots += 1
                total += hint
                for l in shot["dialogue"]:
                    lines += 1
                    per_speaker[names[l["speaker"]]] += 1
                    speech += speech_seconds(l)
        speakers = "，".join(f"{k} {v} 句" for k, v in per_speaker.most_common())
        out += [
            "",
            "### 统计",
            "",
            f"- 场 {len(ep['scenes'])}，镜头 {shots}，台词 {lines}（{speakers}）",
            f"- 镜头时长合计 {total:g} 秒（目标 {ep['target_duration_s']:g} 秒）",
            f"- 台词估算朗读 {speech:.1f} 秒（按每秒 {CHARS_PER_SECOND:g} 字）",
        ]
    return "\n".join(out) + "\n"
