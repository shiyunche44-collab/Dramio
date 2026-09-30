"""keyframe：DramaIR 镜头 + P0-06 定妆参考图 → 9:16 首帧（P0-07，方舟 Seedream 5.0 pro）。

输入固定为标准样例 ep01（D-002），字段运行时读取：series.visual_style、scene.setting、shot.framing / description / lighting、
shot.characters[].action / emotion、characters[].appearance / costume（INV-02，不另建结构）。提示词模板见 poc/prompts/keyframe.v1.md。

三种一致性方案（每个带角色的镜头一张首帧）：
- text  无参考图，人物只靠文字描述；
- ref1  每个出镜角色 1 张参考图：P0-06 选定的 pro 主图 main-02（D-006）；
- ref2  每个出镜角色 2 张：main-02 + derive-ref-pro 的 expression-neutral 正面特写。
参考图顺序固定：按 characters[]（ep01 里苏晚、陆沉）的顺序，每个角色的参考图相邻（ref2 的 4 张为 苏晚主图、苏晚特写、陆沉主图、陆沉特写），
提示词写明“图1是苏晚的全身定妆照，图2是…”，并声明参考图只用于人物身份，场景、姿态、构图、服装以文字为准。
空镜（没有角色）各方案共用 1 张（scheme=shared，按镜头去重），只描述场景、画面中不得有人。

参考图不写死路径：按 P0-06 证据目录里的 manifest 选定（main-pro 的 main-02、derive-ref-pro 的 expression-neutral），并校验文件 sha256。
尺寸 1152×2048（复用 costume.SIZES["portrait"]），watermark=false（D-005），只用 pro，图片以 b64_json 返回、不下载 URL。
计价、重试、费用保险、账号级错误中止、calls 记账与 costume 完全一致（复用 costume.Runner）：node_key 为请求内容的 sha256（只记录，不做缓存）。

镜头可度量性预标注（读 framing 的景别 / 机位与角色数，不写死镜头 id；最终以 face 命令的检测结果为准）：
yes 可度量、maybe 可能不可度量（大特写 / 手部特写、中远景以远、俯拍）、no 不可度量（空镜）。

离线模式：
- --dry-run：打印每个请求的提示词、参考图（路径 / sha256）、预计费用与 node_key，不发请求、不需要密钥；
- --export 运行目录 目标目录：第 1 轮图像 + manifest.json（含 shot_id、characters、scheme、sha256、参考图 sha256、node_key、
  measurable 预标注与默认 false 的 selected）+ run-summary.json + run-calls.jsonl；
- --verify 证据根目录：复核 sha256 / 字节数 / 宽高、manifest 与 summary / calls 的对应、参考图数量、费用合计、无密钥与 base64 数据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from poc import costume, images, pricing, providers, script, seedream
from poc.runlog import Run

PROMPT_VERSION = "keyframe.v1"
SCHEMES = ("text", "ref1", "ref2")
SHARED = "shared"  # 空镜的方案名：各方案共用
MODEL = "pro"
MAX_COST_CNY = 20.0
KIND = "keyframe"
SIZE = costume.SIZES["portrait"]  # 1152x2048
SIZE_WH = (1152, 2048)
# 参考图选定（D-006）：P0-06 证据目录内，按 manifest 取 main-pro 的 main-02 与 derive-ref-pro 的 expression-neutral
DEFAULT_REF_ROOT = script.REPO_ROOT / "docs" / "reports" / "p0" / "P0-06"
REF_SOURCES = {
    "main": ("main-pro", "main", "02"),  # (证据目录, kind, label)
    "neutral": ("derive-ref-pro", "expression", "neutral"),
}
REF_KINDS = {"ref1": ("main",), "ref2": ("main", "neutral")}
REF_KIND_ZH = {"main": "全身定妆照", "neutral": "正面特写"}

SHOT_SIZE_ZH = {
    "ECU": "大特写", "CU": "特写（头部）", "MCU": "近景（胸部以上）", "MS": "中景（腰部以上）",
    "MLS": "中远景（膝盖以上）", "LS": "远景（全身）", "ELS": "大远景",
}
ANGLE_ZH = {"eye_level": "平视", "high": "俯拍", "low": "仰拍", "overhead": "垂直俯拍", "dutch": "倾斜构图"}
INT_EXT_ZH = {"INT": "室内", "EXT": "室外"}
TIME_OF_DAY_ZH = {"day": "白天", "night": "夜晚", "dawn": "黎明", "dusk": "黄昏"}
GENDER_ZH = costume.GENDER_ZH

# 可度量性预标注规则：景别 / 机位 / 动作描述 + 角色数
FAR_SIZES = ("MLS", "LS", "ELS")
TOP_ANGLES = ("high", "overhead")
HAND_MARKERS = ("只露出手", "手部特写")
MEASURABLE_YES, MEASURABLE_MAYBE, MEASURABLE_NO = "yes", "maybe", "no"


class RefError(Exception):
    """参考图不可用：缺 manifest、manifest 里没有选定项、文件缺失或 sha256 不符。"""


# ---- 输入与规划 ----


def shots_of(doc: Mapping[str, Any]):
    """按剧本顺序逐个产出 (scene, shot)。"""
    for scene in doc["episodes"][0]["scenes"]:
        for shot in scene["shots"]:
            yield scene, shot


def measurability(shot: Mapping[str, Any]) -> tuple[str, str]:
    """镜头可度量性预标注：(yes / maybe / no, 原因)。只读 framing 与角色数，不看镜头 id。"""
    chars = shot["characters"]
    if not chars:
        return MEASURABLE_NO, "空镜"
    framing = shot["framing"]
    text = shot["description"] + "".join(c["action"] for c in chars)
    reasons = []
    if framing["shot_size"] == "ECU":
        reasons.append("大特写（ECU），可能只拍到手或局部")
    if any(m in text for m in HAND_MARKERS):
        reasons.append("描述为手部特写，人脸可能不入画")
    if framing["shot_size"] in FAR_SIZES:
        reasons.append(f"景别 {framing['shot_size']} 偏远，人脸像素可能过小")
    if framing["angle"] in TOP_ANGLES:
        reasons.append(f"机位 {framing['angle']}（俯拍），人脸可能被遮挡或偏小")
    return (MEASURABLE_MAYBE, "；".join(reasons)) if reasons else (MEASURABLE_YES, "")


def shot_vars(doc: Mapping[str, Any], scene: Mapping[str, Any], shot: Mapping[str, Any]) -> dict[str, str]:
    setting, framing = scene["setting"], shot["framing"]
    return {
        "style": doc["series"]["visual_style"],
        "location": setting["location"],
        "int_ext": INT_EXT_ZH[setting["int_ext"]],
        "time_of_day": TIME_OF_DAY_ZH[setting["time_of_day"]],
        "scene_description": setting["description"],
        "shot_size": SHOT_SIZE_ZH[framing["shot_size"]],
        "angle": ANGLE_ZH[framing["angle"]],
        "composition": framing["composition"],
        "description": shot["description"],
        "lighting": shot["lighting"],
    }


def _squeeze(text: str) -> str:
    """丢弃空行（text 方案的 $reference 整行为空）。"""
    return "\n".join(ln for ln in text.splitlines() if ln.strip())


def reference_plan(doc: Mapping[str, Any], char_ids: list[str], scheme: str) -> list[tuple[str, str]]:
    """(角色 id, 参考图种类) 列表，即发给模型的图序：按 characters[] 的顺序，每个角色的参考图相邻。"""
    order = [c["id"] for c in doc["characters"]]
    return [(cid, kind) for cid in sorted(char_ids, key=order.index) for kind in REF_KINDS.get(scheme, ())]


@dataclass
class Job(costume.Job):
    """一个首帧请求：char_id = shot_id，kind = keyframe，label = 方案（text / ref1 / ref2 / shared）。"""

    characters: list[str] = field(default_factory=list)
    measurable: str = ""
    measurable_reason: str = ""

    @property
    def shot_id(self) -> str:
        return self.char_id

    @property
    def scheme(self) -> str:
        return self.label


def parse_schemes(spec: str) -> list[str]:
    """'text,ref1' / 'all' → 按 SCHEMES 顺序去重的方案列表；不认识的方案抛 ValueError。"""
    names = [x.strip() for x in spec.split(",") if x.strip()]
    if names == ["all"]:
        return list(SCHEMES)
    bad = [x for x in names if x not in SCHEMES]
    if bad or not names:
        raise ValueError(f"--scheme 必须是 {' / '.join(SCHEMES)}（可用逗号组合，或 all）：{spec!r}")
    return [s for s in SCHEMES if s in names]


def plan_jobs(
    doc: Mapping[str, Any],
    schemes: list[str],
    *,
    shots: list[str] | None = None,
    library: Mapping[str, Mapping[str, costume.Ref]] | None = None,
    version: str = PROMPT_VERSION,
    size: str = SIZE,
) -> list[Job]:
    """空镜（各方案共用 1 张）在前，其后按方案、镜头顺序。shots 为空表示全部镜头。"""
    library = library or {}
    t = costume.load_templates(version)
    names = {c["id"]: c for c in doc["characters"]}
    wanted = set(shots or [])
    empty: list[Job] = []
    per_scheme: dict[str, list[Job]] = {s: [] for s in schemes}
    for scene, shot in shots_of(doc):
        if wanted and shot["shot_id"] not in wanted:
            continue
        v = shot_vars(doc, scene, shot)
        status, reason = measurability(shot)
        ids = [c["character_id"] for c in shot["characters"]]
        if not ids:
            empty.append(Job(shot["shot_id"], KIND, SHARED, "empty", _squeeze(t["empty"].substitute(v)), size, [], [], status, reason))
            continue
        lines = []
        for c in shot["characters"]:
            ch = names[c["character_id"]]
            lines.append(t["character"].substitute(
                name=ch["name"], age=str(ch["age"]), gender=GENDER_ZH.get(ch["gender"], ""), appearance=ch["appearance"],
                costume=ch["costume"], action=c["action"], emotion=c["emotion"],
            ))
        for scheme in schemes:
            plan = reference_plan(doc, ids, scheme)
            try:
                refs = [library[cid][kind] for cid, kind in plan]
            except KeyError as exc:
                raise ValueError(f"{scheme} 方案缺少参考图 {exc}，需要 library（见 load_library）") from None
            mapping = "，".join(f"图{i}是{names[cid]['name']}的{REF_KIND_ZH[kind]}" for i, (cid, kind) in enumerate(plan, 1))
            prompt = _squeeze(t["shot"].substitute(
                v, characters="\n".join(lines), reference=t["reference"].substitute(mapping=mapping) if plan else "",
            ))
            per_scheme[scheme].append(Job(shot["shot_id"], KIND, scheme, "shot", prompt, size, refs, ids, status, reason))
    return empty + [j for s in schemes for j in per_scheme[s]]


def load_library(root: Path, char_ids: list[str]) -> dict[str, dict[str, costume.Ref]]:
    """{角色 id: {main: Ref, neutral: Ref}}：按 P0-06 证据目录的 manifest 选定参考图，并校验 sha256。"""
    library: dict[str, dict[str, costume.Ref]] = {cid: {} for cid in char_ids}
    for kind, (dirname, m_kind, m_label) in REF_SOURCES.items():
        mpath = root / dirname / "manifest.json"
        if not mpath.exists():
            raise RefError(f"缺少 P0-06 manifest：{mpath}（用 --ref-root 指定 P0-06 证据目录）")
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        for cid in char_ids:
            entry = next(
                (e for e in manifest["images"] if e["char_id"] == cid and e["kind"] == m_kind and e["label"] == m_label and e.get("committed", True)),
                None,
            )
            if entry is None:
                raise RefError(f"{mpath} 中没有 {cid} 的 {m_kind}-{m_label}")
            path = mpath.parent / entry["file"]
            try:
                ref = costume.load_ref(path)
            except (OSError, images.ImageFormatError) as exc:
                raise RefError(f"参考图不可用 {path}：{exc}") from None
            if ref.sha256 != entry["sha256"]:
                raise RefError(f"参考图 {path} 的 sha256 与 manifest 不一致")
            library[cid][kind] = ref
    return library


def library_info(library: Mapping[str, Mapping[str, costume.Ref]]) -> dict[str, dict[str, dict[str, str]]]:
    return {cid: {k: {"path": r.path, "sha256": r.sha256} for k, r in refs.items()} for cid, refs in library.items()}


# ---- 生成 ----


@dataclass
class Settings:
    schemes: list[str]
    shots: list[str] = field(default_factory=list)  # 空 = 全部镜头
    rounds: int = 1
    max_cost_cny: float = MAX_COST_CNY
    ref_root: str | None = None  # 默认 P0-06 证据目录
    prompt_version: str = PROMPT_VERSION
    name: str | None = None
    model: str = MODEL

    @property
    def model_id(self) -> str:
        return seedream.MODELS[self.model]

    @property
    def label(self) -> str:
        return self.name or f"{KIND}-{'-'.join(self.schemes)}"


@dataclass
class Item(costume.Item):
    shot_id: str = ""
    scheme: str = ""
    characters: list[str] = field(default_factory=list)
    measurable: str = ""
    measurable_reason: str = ""


class Runner(costume.Runner):
    def new_item(self, job: costume.Job, rnd: int, key: str) -> Item:
        base = super().new_item(job, rnd, key)
        assert isinstance(job, Job)
        return Item(
            **asdict(base), shot_id=job.shot_id, scheme=job.scheme, characters=list(job.characters),
            measurable=job.measurable, measurable_reason=job.measurable_reason,
        )

    def call_extra(self, job: costume.Job, rnd: int, retry: int) -> dict[str, Any]:
        assert isinstance(job, Job)
        return {**super().call_extra(job, rnd, retry), "shot_id": job.shot_id, "scheme": job.scheme}


def _summarize(settings: Settings, items: list[costume.Item], n_expected: int) -> dict[str, Any]:
    summary = costume.summarize(settings, items, n_expected)  # type: ignore[arg-type]
    rounds = max(settings.rounds, 1)
    # costume 按 char_id（此处即 shot_id）分组，改名为 per_shot_per_round，另加按方案分组
    summary["per_shot_per_round"] = summary.pop("per_char_per_round")
    per_scheme: dict[str, dict[str, Any]] = {}
    for i in items:
        ps = per_scheme.setdefault(i.scheme, {"items": 0, "ok": 0, "cost_cny": 0.0, "elapsed_s": 0.0})  # type: ignore[attr-defined]
        ps["items"] += 1
        ps["ok"] += int(i.ok)
        ps["cost_cny"] = round(ps["cost_cny"] + i.cost_cny, 6)
        ps["elapsed_s"] = round(ps["elapsed_s"] + i.elapsed_s, 3)
    summary["per_scheme"] = {k: {**v, "cost_cny_per_round": round(v["cost_cny"] / rounds, 6)} for k, v in per_scheme.items()}
    return summary


def _source(episode: Path, sha: str) -> dict[str, str]:
    return {"path": str(episode.relative_to(script.REPO_ROOT)) if episode.is_relative_to(script.REPO_ROOT) else str(episode), "sha256": sha}


def _prepare(settings: Settings, doc: Mapping[str, Any], out: TextIO) -> tuple[list[Job], dict[str, dict[str, costume.Ref]]] | None:
    """校验输入并规划任务；输入有误时打印原因并返回 None（退出码 2）。"""
    known = [sh["shot_id"] for _, sh in shots_of(doc)]
    unknown = [s for s in settings.shots if s not in known]
    if unknown:
        print(f"未知镜头：{', '.join(unknown)}（可选 {', '.join(known)}）", file=out)
        return None
    bad = [s for s in settings.schemes if s not in SCHEMES]
    if bad or not settings.schemes:
        print(f"--scheme 必须是 {' / '.join(SCHEMES)}", file=out)
        return None
    if settings.rounds < 1:
        print("--rounds 至少为 1", file=out)
        return None
    library: dict[str, dict[str, costume.Ref]] = {}
    wanted = set(settings.shots)
    need_refs = any(s in REF_KINDS for s in settings.schemes) and any(
        sh["characters"] for _, sh in shots_of(doc) if not wanted or sh["shot_id"] in wanted
    )
    if need_refs:
        try:
            library = load_library(Path(settings.ref_root) if settings.ref_root else DEFAULT_REF_ROOT, [c["id"] for c in doc["characters"]])
        except RefError as exc:
            print(str(exc), file=out)
            return None
    jobs = plan_jobs(doc, settings.schemes, shots=settings.shots, library=library or None, version=settings.prompt_version)
    if not jobs:
        print("没有要生成的镜头", file=out)
        return None
    return jobs, library


def run_keyframe(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
    out: TextIO | None = None,
    episode: Path = script.SAMPLE_EP01,
    gen_fn: costume.GenerateFn | None = None,
    sleep=time.sleep,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    if not (env.get(seedream.KEY_ENV) or "").strip():
        print(f"缺少 {seedream.KEY_ENV}：在云环境设置或 spikes/poc/.env 中配置", file=out)
        return 2
    try:
        billing = seedream.billing_mode(env)
        seedream.check_model(settings.model_id, billing)
    except seedream.ImageError as exc:
        print(str(exc), file=out)
        return 2
    doc, sha = costume.load_episode(episode)
    prepared = _prepare(settings, doc, out)
    if prepared is None:
        return 2
    jobs, library = prepared
    source = _source(episode, sha)
    args = {"source": source, **asdict(settings), "references": library_info(library), "watermark": costume.WATERMARK, "billing": billing}
    with Run(KIND, args, base_dir=base_dir, secrets=providers.secret_values(env)) as run:
        runner = Runner(run, settings, env, gen_fn=gen_fn, sleep=sleep)
        print(
            f"run_id: {run.run_id}  方案: {settings.label}  模型: {settings.model_id}（{billing}）  方案集: {', '.join(settings.schemes)}  每轮 {len(jobs)} 张",
            file=out,
        )
        items, aborted = costume.execute_jobs(runner, jobs, settings.rounds, run, out)
        summary = _summarize(settings, items, len(jobs) * settings.rounds)
        summary.update(
            run_id=run.run_id,
            source=source,
            references=library_info(library),
            watermark=costume.WATERMARK,
            output_format=costume.OUTPUT_FORMAT,
            prompt_version=settings.prompt_version,
            billing=billing,
            aborted=aborted,
            spent_cny_total=round(runner.spent, 6),
            price=costume.price_block(settings.model_id, billing),
        )
        run.write_json("summary.json", summary)
        ok = aborted is None and summary["ok"] == summary["n_expected"]
        print(
            f"\n成功 {summary['ok']}/{summary['n_expected']}（首次成功 {summary['first_ok']}），瞬时失败 {summary['transient_failures']}，"
            f"失败类型 {summary['failures_by_kind'] or '无'}，估算费用 ¥{summary['cost_cny_total']:.2f}",
            file=out,
        )
        print(f"输出：{run.dir}", file=out)
        if not ok:
            run.meta["result"] = "failed"
    return 0 if ok else 1


def dry_run(settings: Settings, env: Mapping[str, str] | None = None, out: TextIO | None = None, episode: Path = script.SAMPLE_EP01) -> int:
    """离线：打印每个请求的提示词、参考图、预计费用与 node_key（第 1 轮），不发请求、不需要密钥。"""
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    try:
        billing = seedream.billing_mode(env)
        seedream.check_model(settings.model_id, billing)
    except seedream.ImageError as exc:
        print(str(exc), file=out)
        return 2
    doc, sha = costume.load_episode(episode)
    prepared = _prepare(settings, doc, out)
    if prepared is None:
        return 2
    jobs, _ = prepared
    names = {c["id"]: c["name"] for c in doc["characters"]}
    print(f"dry-run（不发请求）  方案集: {', '.join(settings.schemes)}  模型: {settings.model_id}（{billing}）  尺寸: {SIZE}  watermark={costume.WATERMARK}", file=out)
    print(f"输入: {_source(episode, sha)['path']}  sha256={sha}  提示词: {settings.prompt_version}", file=out)
    total = 0.0
    for n, job in enumerate(jobs, 1):
        price = pricing.image_cny(seedream.PROVIDER, settings.model_id, pricing.size_pixels(job.size), len(job.refs))
        total += price
        who = "、".join(names[c] for c in job.characters) or "无（空镜）"
        print(f"\n[{n}/{len(jobs)}] {job.shot_id}  方案 {job.scheme}  角色: {who}", file=out)
        print(f"  可度量预标注: {job.measurable}{'（' + job.measurable_reason + '）' if job.measurable_reason else ''}", file=out)
        print(f"  node_key(r1): {costume.node_key(settings.model_id, job, 'r1')}", file=out)
        print(f"  预计费用: ¥{price:.2f}（{job.size}，参考图 {len(job.refs)} 张）", file=out)
        for i, r in enumerate(job.refs, 1):
            print(f"  图{i}: {r.path}  sha256={r.sha256}", file=out)
        print("  提示词:", file=out)
        for line in job.prompt.splitlines():
            print(f"    {line}", file=out)
    print(f"\n每轮 {len(jobs)} 张，预计 ¥{total:.2f}；{settings.rounds} 轮共 ¥{total * settings.rounds:.2f}（上限 ¥{settings.max_cost_cny:g}）", file=out)
    if total * settings.rounds > settings.max_cost_cny + 1e-9:
        print("注意：预计费用超过 --max-cost-cny，实际运行会在超出前中止", file=out)
    return 0


# ---- 离线：--export / --verify ----


def export_run(run_dir: Path, dest: Path, rnd: int = 1, out: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    summary_path = run_dir / "summary.json"
    if not summary_path.exists() or not (run_dir / "calls.jsonl").exists():
        print(f"{run_dir} 不是完整的 keyframe 运行目录（缺少 summary.json 或 calls.jsonl）", file=out)
        return 2
    if dest.exists() and any(dest.iterdir()):
        print(f"目标目录非空：{dest}", file=out)
        return 2
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    dest.mkdir(parents=True, exist_ok=True)
    entries = []
    for it in summary["items"]:
        if it["round"] != rnd or not it["ok"]:
            continue
        src = run_dir / it["file"]
        rel = Path(it["shot_id"]) / f"{KIND}-{it['scheme']}-{it['sha256'][:8]}{src.suffix}"
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / rel)
        entries.append({
            "shot_id": it["shot_id"], "characters": it["characters"], "scheme": it["scheme"], "round": it["round"],
            "file": rel.as_posix(), "sha256": it["sha256"], "bytes": it["bytes"], "width": it["width"], "height": it["height"],
            "reported_size": it["reported_size"], "request_id": it["request_id"], "node_key": it["node_key"],
            "size": it["size"], "refs": it["refs"],
            "measurable": it["measurable"], "measurable_reason": it["measurable_reason"],
            "selected": False, "committed": True,
        })
    manifest = {
        "run_id": summary["run_id"],
        "candidate": summary["candidate"],
        "stage": KIND,
        "schemes": summary["settings"]["schemes"],
        "model_id": summary["model_id"],
        "prompt_version": summary["prompt_version"],
        "watermark": summary["watermark"],
        "output_format": summary["output_format"],
        "billing": summary.get("billing", "payg"),
        "source": summary["source"],
        "references": summary["references"],
        "round": rnd,
        "images": entries,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(summary_path, dest / "run-summary.json")
    shutil.copyfile(run_dir / "calls.jsonl", dest / "run-calls.jsonl")
    print(f"已导出 {len(entries)} 张到 {dest}", file=out)
    return 0


def verify_dir(d: Path, require_selected: bool = False) -> list[str]:
    """复核一个 keyframe 证据目录（含 manifest.json、run-summary.json、run-calls.jsonl）。返回问题列表。"""
    problems: list[str] = []
    where = d.name
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((d / "run-summary.json").read_text(encoding="utf-8"))
    entries = manifest["images"]
    if manifest.get("stage") != KIND:
        problems.append(f"{where}：manifest 的 stage 不是 {KIND}")
    if manifest["run_id"] != summary["run_id"]:
        problems.append(f"{where}：manifest 与 run-summary 的 run_id 不一致")
    if manifest.get("watermark") is not False:
        problems.append(f"{where}：watermark 应为 false（D-005）")
    src = manifest.get("source") or {}
    src_path = script.REPO_ROOT / src.get("path", "")
    if src.get("path") and src_path.is_file() and hashlib.sha256(src_path.read_bytes()).hexdigest() != src.get("sha256"):
        problems.append(f"{where}：输入 {src['path']} 的 sha256 与 manifest 不一致")
    costume.check_files(d, entries, where, problems)
    keys = [(e["shot_id"], e["scheme"]) for e in entries]
    if len(set(keys)) != len(keys):
        problems.append(f"{where}：manifest 中 (shot_id, scheme) 有重复")
    listed = {(e["shot_id"], e["scheme"]): e["sha256"] for e in entries}
    ok_items = {(i["shot_id"], i["scheme"]): i["sha256"] for i in summary["items"] if i["ok"] and i["round"] == manifest["round"]}
    if listed != ok_items:
        problems.append(f"{where}：manifest 与 run-summary 第 {manifest['round']} 轮成功项不一致（{len(listed)} / {len(ok_items)}）")
    for e in entries:
        name = f"{e['shot_id']}/{e['scheme']}"
        if (e["width"], e["height"]) != SIZE_WH:
            problems.append(f"{where}：{name} 尺寸 {e['width']}x{e['height']}，应为 {SIZE}")
        if e["scheme"] in REF_KINDS:
            expect = len(e["characters"]) * len(REF_KINDS[e["scheme"]])
        else:
            expect = 0 if e["scheme"] in ("text", SHARED) else None
        if expect is None:
            problems.append(f"{where}：{name} 的方案未知")
        elif len(e["refs"]) != expect:
            problems.append(f"{where}：{name} 有 {len(e['refs'])} 张参考图，方案 {e['scheme']} 应为 {expect} 张")
        if (e["scheme"] == SHARED) != (not e["characters"]) or (e["measurable"] == MEASURABLE_NO) != (not e["characters"]):
            problems.append(f"{where}：{name} 的 characters、scheme、measurable 互相矛盾（空镜应为 shared / no）")
        for r in e["refs"]:
            path = script.REPO_ROOT / r["path"]
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != r["sha256"]:
                problems.append(f"{where}：{name} 的参考图 {r['path']} 与记录的 sha256 不一致")
        if not isinstance(e.get("selected"), bool):
            problems.append(f"{where}：{name} 缺少布尔字段 selected")
    by_shot: dict[str, int] = {}
    for e in entries:
        by_shot[e["shot_id"]] = by_shot.get(e["shot_id"], 0) + int(e.get("selected") is True)
    for shot_id, n in sorted(by_shot.items()):
        if n > 1:
            problems.append(f"{where}：{shot_id} 选定了 {n} 张首帧（selected 每个镜头最多 1 张）")
        if n == 0 and require_selected:
            problems.append(f"{where}：{shot_id} 没有选定首帧（selected）")
    costume.check_calls(d, manifest, summary, where, problems)
    return problems


def run_verify(root: Path, out: TextIO | None = None, require_selected: bool = False) -> int:
    out = sys.stdout if out is None else out
    code = costume.run_verify(root, out, verify=lambda d: verify_dir(d, require_selected))
    selected = shots = 0
    for m in sorted(root.rglob("manifest.json")):
        entries = json.loads(m.read_text(encoding="utf-8"))["images"]
        shots += len({e["shot_id"] for e in entries})
        selected += len({e["shot_id"] for e in entries if e.get("selected") is True})
    if shots:
        print(f"已选定首帧 {selected}/{shots} 个镜头", file=out)
    return code


# ---- CLI ----


def _cmd(args: argparse.Namespace) -> int:
    generation = [o for o, v in (("--scheme", args.scheme), ("--shots", args.shots), ("--rounds", args.rounds), ("--name", args.name),
                                  ("--ref-root", args.ref_root), ("--dry-run", args.dry_run or None)) if v is not None]
    offline = [o for o, v in (("--export", args.export), ("--verify", args.verify)) if v is not None]
    if offline:
        if len(offline) > 1:
            args.parser.error(f"离线模式只能选一个：{', '.join(offline)}")
        if generation:
            args.parser.error(f"离线模式不能与生成参数一起用：{', '.join(generation)}")
        if args.export is not None:
            if args.require_selected:
                args.parser.error("--require-selected 只能与 --verify 一起用")
            return export_run(Path(args.export[0]), Path(args.export[1]))
        return run_verify(Path(args.verify), require_selected=args.require_selected)
    if args.require_selected:
        args.parser.error("--require-selected 只能与 --verify 一起用")
    if args.scheme is None:
        args.parser.error("生成模式需要 --scheme")
    try:
        schemes = parse_schemes(args.scheme)
    except ValueError as exc:
        args.parser.error(str(exc))
    settings = Settings(
        schemes=schemes,
        shots=[s.strip() for s in (args.shots or "").split(",") if s.strip()],
        rounds=1 if args.rounds is None else args.rounds,
        max_cost_cny=args.max_cost_cny,
        ref_root=args.ref_root,
        name=args.name,
    )
    return dry_run(settings) if args.dry_run else run_keyframe(settings)


def add_parser(sub) -> None:
    p = sub.add_parser("keyframe", help="DramaIR 镜头 + 定妆参考图 → 9:16 首帧，比较三种一致性方案（P0-07，方舟 Seedream 5.0 pro）")
    p.add_argument("--scheme", metavar="方案", help="text 无参考图 / ref1 每角色 1 张参考图 / ref2 每角色 2 张；可用逗号组合或 all（空镜各方案共用 1 张）")
    p.add_argument("--shots", metavar="id,id", help="只生成这些镜头（默认全部），例如 ep01_sc01_sh03,ep01_sc01_sh05")
    p.add_argument("--rounds", type=int, default=None, help="轮数（默认 1）")
    p.add_argument("--ref-root", metavar="目录", help="P0-06 证据目录（默认 docs/reports/p0/P0-06，按其 manifest 选定 main-02 与 expression-neutral）")
    p.add_argument("--name", help="方案名（默认 keyframe-<方案集>）")
    p.add_argument("--max-cost-cny", type=float, default=MAX_COST_CNY, help=f"本次运行累计估算费用上限（元），预计超出即中止（默认 {MAX_COST_CNY:g}）")
    p.add_argument("--dry-run", action="store_true", help="离线：打印每个请求的提示词、参考图、预计费用与 node_key，不发请求")
    p.add_argument("--export", nargs=2, metavar=("运行目录", "目标目录"), help="离线：把一次运行的第 1 轮整理成入库证据（selected 默认 false）")
    p.add_argument("--verify", metavar="证据根目录", help="离线：复核证据目录（sha256、尺寸、manifest 与 calls 对应、参考图数量、费用）")
    p.add_argument("--require-selected", action="store_true", help="与 --verify 同用：每个镜头必须有 1 张 selected 首帧")
    p.set_defaults(func=_cmd, parser=p)
