"""costume：DramaIR 角色描述 → 定妆主图、三视图、表情集（P0-06，方舟 Seedream 5.0）。

两个阶段：
- main：每个角色 n 张文生图主图（竖构图 9:16 全身），用于挑选身份参考；
- derive：以选定的主图为基础派生三视图（正 / 左侧 / 背）与表情集，三种方式：
  - text  每张独立文生图，完整重复角色描述（Seedance 可信的文生图原始产物）；
  - ref   以主图为唯一参考图的图生图（一致性通常更好，但不是文生图，Seedance 不接受）；
  - sheet 两张文生图设定板：三视图横排一张、表情网格一张（5.0 pro / flash 不支持组图，用设定板代替）。
表情集 = neutral + 该角色在 ep01 台词 delivery.emotion 中出现过的值（按首次出现顺序，最多 MAX_EXPRESSIONS 个）。
提示词模板见 poc/prompts/costume.v1.md，字段来自 series.visual_style 与 characters[]（INV-02）。

每个请求经 Run.call 记账（provider=ark，capability=image，cost_basis=estimate），node_key 为请求内容的 sha256（只记录，不做缓存）。
瞬时故障（网络、429、5xx）同一张最多重试 TRANSIENT_RETRIES 次。费用口径见 seedream.ImageError.billable。
所有请求 watermark=false（D-005：定妆图是内部中间资产，成片的 AIGC 标识由 P0-11 负责）。
calls.jsonl 与 summary 不写图像数据；图像保存在 runs/<run_id>/images/r<轮>/<角色>/，字节原样不改动。

离线模式：
- --export 运行目录 目标目录：第 1 轮图像（文件名带 sha256 前 8 位）+ manifest.json + run-summary.json + run-calls.jsonl；
- --verify 证据根目录：复核每个 manifest 的图像 sha256 / 字节数 / 宽高、与 summary 和 calls 的对应、Seedance 可用性标注、费用合计；
- --cards 证据根目录：按角色生成定妆卡 <角色 id>.md（markdown 并排展示，不拼图）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import string
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from poc import images, pricing, providers, script, seedream
from poc.runlog import Run

PROMPT_VERSION = "costume.v1"
PROMPT_VERSIONS = ("costume.v1", "costume.v2")
TRANSIENT_RETRIES = 2
MAX_EXPRESSIONS = 6
WATERMARK = False  # D-005
OUTPUT_FORMAT = "jpeg"
SIZES = {"portrait": "1152x2048", "landscape": "2048x1152", "square": "1536x1536"}  # 均 ≤261 万像素（pro 低价档）
# --small：接口允许的最小像素数（921600），用于派生图，控制入库体积（主图不用）
SMALL_SIZES = {"portrait": "720x1280", "landscape": "1280x720", "square": "960x960"}
VIEWS = [("front", "正面"), ("side", "左侧面"), ("back", "背面")]
GENDER_ZH = {"female": "女性", "male": "男性"}
EXPRESSION_ZH: dict[str, str] = {
    "neutral": "平静自然，嘴唇放松，目视镜头",
    "happy": "得意的笑，嘴角上扬",
    "sad": "委屈难过，眼眶泛红、强忍泪水",
    "angry": "愤怒，眉头紧锁、咬紧嘴唇",
    "fearful": "惊慌失措，瞪大眼睛、脸色发白",
    "surprised": "震惊错愕，眼睛睁大、嘴微张",
    "disgusted": "厌恶嫌弃，皱鼻撇嘴",
    "tender": "温柔，眼神柔和、浅浅微笑",
    "anxious": "警觉不安，眉头微蹙、眼神紧张",
    "sarcastic": "讥讽，似笑非笑、单侧嘴角上挑",
}
MODES = ("text", "ref", "sheet")
MAX_CALL_LINE_BYTES = 16 * 1024


# ---- 模板与输入 ----


def load_templates(version: str = PROMPT_VERSION) -> dict[str, string.Template]:
    text = (script.PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^## (\w+)\s*$", line)
        if m:
            current = m.group(1)
            sections[current] = []
        elif current is not None and not line.startswith(">"):
            sections[current].append(line)
    return {k: string.Template("\n".join(v).strip()) for k, v in sections.items()}


def load_episode(path: Path = script.SAMPLE_EP01) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def character(doc: Mapping[str, Any], char_id: str) -> dict[str, Any]:
    for c in doc["characters"]:
        if c["id"] == char_id:
            return c
    raise KeyError(char_id)


def character_vars(doc: Mapping[str, Any], char_id: str) -> dict[str, str]:
    c = character(doc, char_id)
    return {
        "style": doc["series"]["visual_style"],
        "name": c["name"],
        "gender": GENDER_ZH.get(c["gender"], ""),
        "age": str(c["age"]),
        "appearance": c["appearance"],
        "costume": c["costume"],
    }


def expressions_for(doc: Mapping[str, Any], char_id: str) -> list[str]:
    """neutral + 该角色台词 delivery.emotion（按首次出现顺序去重），最多 MAX_EXPRESSIONS 个。"""
    out = ["neutral"]
    for scene in doc["episodes"][0]["scenes"]:
        for shot in scene["shots"]:
            for line in shot.get("dialogue") or []:
                emo = line["delivery"]["emotion"]
                if line["speaker"] == char_id and emo not in out:
                    out.append(emo)
    return out[:MAX_EXPRESSIONS]


# ---- 任务规划 ----


@dataclass
class Ref:
    path: str
    sha256: str
    fmt: str
    data: bytes = field(repr=False, default=b"")


@dataclass
class Job:
    char_id: str
    kind: str  # main / view / expression / sheet
    label: str  # 01 / front / angry / views / expressions
    section: str
    prompt: str
    size: str
    refs: list[Ref] = field(default_factory=list)


def plan_jobs(
    doc: Mapping[str, Any],
    stage: str,
    chars: list[str],
    *,
    n: int = 3,
    mode: str | None = None,
    bases: Mapping[str, Ref] | None = None,
    version: str = PROMPT_VERSION,
    sizes: Mapping[str, str] = SIZES,
) -> list[Job]:
    t = load_templates(version)
    jobs: list[Job] = []
    for cid in chars:
        v = character_vars(doc, cid)
        if stage == "main":
            prompt = t["main"].substitute(v)
            jobs += [Job(cid, "main", f"{i:02d}", "main", prompt, sizes["portrait"]) for i in range(1, n + 1)]
            continue
        exprs = expressions_for(doc, cid)
        if mode == "sheet":
            jobs.append(Job(cid, "sheet", "views", "view_sheet", t["view_sheet"].substitute(v), sizes["landscape"]))
            jobs.append(Job(
                cid, "sheet", "expressions", "expression_sheet",
                t["expression_sheet"].substitute(v, expressions="、".join(EXPRESSION_ZH[e].split("，")[0] for e in exprs), count=len(exprs)),
                sizes["square"],
            ))
            continue
        refs = [bases[cid]] if mode == "ref" else []
        suffix = "_ref" if mode == "ref" else ""
        for key, zh in VIEWS:
            jobs.append(Job(cid, "view", key, "view" + suffix, t["view" + suffix].substitute(v, view=zh), sizes["portrait"], list(refs)))
        for e in exprs:
            jobs.append(Job(cid, "expression", e, "expression" + suffix, t["expression" + suffix].substitute(v, expression=EXPRESSION_ZH[e]), sizes["portrait"], list(refs)))
    return jobs


def node_key(model: str, job: Job, sample: str) -> str:
    """请求内容的 sha256：模型、提示词、尺寸、水印、输出格式、参考图 sha256、采样序号（无 seed，同一请求多次采样结果不同）。"""
    payload = {
        "provider": seedream.PROVIDER,
        "model": model,
        "prompt": job.prompt,
        "size": job.size,
        "watermark": WATERMARK,
        "output_format": OUTPUT_FORMAT,
        "refs": [r.sha256 for r in job.refs],
        "sample": sample,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# ---- 生成 ----


@dataclass
class Settings:
    stage: str  # main / derive
    model: str  # pro / flash / v4（plan 模式只能 pro）
    chars: list[str]
    mode: str | None = None  # derive：text / ref / sheet
    n: int = 3
    rounds: int = 1
    bases: dict[str, str] = field(default_factory=dict)  # 角色 → 主图路径
    max_cost_cny: float = 15.0
    prompt_version: str = PROMPT_VERSION
    name: str | None = None
    small: bool = False  # 用 SMALL_SIZES

    @property
    def model_id(self) -> str:
        return seedream.MODELS[self.model]

    @property
    def label(self) -> str:
        return self.name or (f"main-{self.model}" if self.stage == "main" else f"derive-{self.mode}-{self.model}")


@dataclass
class Item:
    round: int
    char_id: str
    kind: str
    label: str
    section: str
    size: str
    prompt: str
    refs: list[dict[str, str]]
    node_key: str
    t2i_original: bool
    ok: bool = False
    error: str | None = None
    error_kind: str | None = None
    requests: int = 0
    first_ok: bool = False
    transient_failures: int = 0
    elapsed_s: float = 0.0  # 含重试
    first_elapsed_s: float | None = None
    cost_cny: float = 0.0
    file: str | None = None  # 相对运行目录
    sha256: str | None = None
    bytes: int | None = None
    width: int | None = None
    height: int | None = None
    reported_size: str | None = None
    request_id: str | None = None

    @property
    def seedance_eligible(self) -> bool:
        return self.ok and self.t2i_original


class Abort(Exception):
    """中止整次运行：费用上限（CostLimit）或账号级错误（欠费、模型未开通、套餐不支持该模型、鉴权失败、配置错误）。"""

    def __init__(self, message: str, item: "Item | None" = None):
        super().__init__(message)
        self.item = item  # 中止时正在处理的一张（已产生的请求与费用要计入 summary）


class CostLimit(Abort):
    pass


GenerateFn = Callable[..., seedream.ImageResult]


class Runner:
    def __init__(self, run: Run, settings: Settings, env: Mapping[str, str], gen_fn: GenerateFn | None = None, sleep=time.sleep):
        self.run = run
        self.s = settings
        self.env = env
        self.gen_fn = gen_fn or seedream.generate
        self.sleep = sleep
        self.spent = 0.0
        self.billing = seedream.billing_mode(env)

    def _price(self, job: Job) -> float:
        return pricing.image_cny(seedream.PROVIDER, self.s.model_id, pricing.size_pixels(job.size), len(job.refs))

    def item(self, job: Job, rnd: int, out_dir: Path) -> Item:
        sample = f"r{rnd}"
        it = Item(
            round=rnd, char_id=job.char_id, kind=job.kind, label=job.label, section=job.section, size=job.size,
            prompt=job.prompt, refs=[{"path": r.path, "sha256": r.sha256} for r in job.refs],
            node_key=node_key(self.s.model_id, job, sample), t2i_original=not job.refs,
        )
        uris = [seedream.data_uri(r.data, r.fmt) for r in job.refs]
        t0 = time.monotonic()
        try:
            for retry in range(TRANSIENT_RETRIES + 1):
                price = self._price(job)
                if self.spent + price > self.s.max_cost_cny + 1e-9:
                    raise CostLimit(f"累计估算费用 ¥{self.spent:.2f} + 本张 ¥{price:.2f} 将超过上限 ¥{self.s.max_cost_cny:g}", it)
                it.requests += 1
                t_req = time.monotonic()
                with self.run.call(seedream.PROVIDER, "image", model=self.s.model_id, node_key=it.node_key) as call:
                    call.cost_basis = "estimate"
                    call.cost_cny = 0.0
                    call.extra = {
                        "candidate": self.s.label, "round": rnd, "char_id": job.char_id, "kind": job.kind, "label": job.label,
                        "retry": retry, "size": job.size, "refs": [r.sha256 for r in job.refs], "billing": self.billing,
                    }
                    try:
                        res = self.gen_fn(
                            job.prompt, model=self.s.model_id, size=job.size, refs=uris or None, watermark=WATERMARK,
                            output_format=OUTPUT_FORMAT, env=self.env,
                        )
                    except seedream.ImageError as exc:
                        call.status = "error"
                        call.error = str(exc)
                        call.extra.update(error_kind=exc.kind, http_status=exc.http_status, api_code=exc.api_code, billable=exc.billable)
                        cost = price if exc.billable else 0.0
                        call.cost_cny = cost
                        self.spent += cost
                        it.cost_cny += cost
                        it.error, it.error_kind = str(exc), exc.kind
                        if exc.fatal:
                            raise Abort(f"账号级错误，中止运行：{exc}", it)
                        if not exc.transient or retry == TRANSIENT_RETRIES:
                            return it
                        it.transient_failures += 1
                        self.sleep(2 ** (retry + 1))
                        continue
                    n_images = int(res.usage.get("generated_images") or len(res.images))
                    cost = pricing.image_cny(seedream.PROVIDER, self.s.model_id, pricing.size_pixels(job.size), len(job.refs), n_images)
                    call.cost_cny = cost
                    self.spent += cost
                    it.cost_cny += cost
                    call.request_id = res.request_id
                    call.usage = dict(res.usage)
                    img = res.images[0]
                    if img.data is None:
                        call.status = "error"
                        call.error = "bad_response: 未返回 b64_json"
                        it.error, it.error_kind = call.error, "bad_response"
                        return it
                    try:
                        info = images.image_info(img.data)
                    except images.ImageFormatError as exc:
                        call.status = "error"
                        call.error = f"bad_response: {exc}"
                        it.error, it.error_kind = call.error, "bad_response"
                        return it
                    path = out_dir / job.char_id / f"{job.kind}-{job.label}.{'jpg' if info.fmt == 'jpeg' else info.fmt}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(img.data)
                    call.extra.update(sha256=info.sha256, bytes=info.bytes, width=info.width, height=info.height, reported_size=img.size)
                    it.ok, it.error, it.error_kind = True, None, None
                    it.first_ok = retry == 0
                    it.first_elapsed_s = round(time.monotonic() - t_req, 3) if retry == 0 else None
                    it.file = str(path.relative_to(self.run.dir))
                    it.sha256, it.bytes, it.width, it.height = info.sha256, info.bytes, info.width, info.height
                    it.reported_size, it.request_id = img.size, res.request_id
                    return it
            return it
        finally:
            it.elapsed_s = round(time.monotonic() - t0, 3)


def summarize(settings: Settings, items: list[Item], n_expected: int) -> dict[str, Any]:
    ok = [i for i in items if i.ok]
    first = [i.first_elapsed_s for i in items if i.first_elapsed_s is not None]
    kinds: dict[str, int] = {}
    for i in items:
        if not i.ok and i.error_kind:
            kinds[i.error_kind] = kinds.get(i.error_kind, 0) + 1
    per_char: dict[str, dict[str, Any]] = {}
    for i in items:
        pc = per_char.setdefault(i.char_id, {"items": 0, "ok": 0, "cost_cny": 0.0, "elapsed_s": 0.0})
        pc["items"] += 1
        pc["ok"] += int(i.ok)
        pc["cost_cny"] = round(pc["cost_cny"] + i.cost_cny, 6)
        pc["elapsed_s"] = round(pc["elapsed_s"] + i.elapsed_s, 3)
    rounds = max(settings.rounds, 1)
    return {
        "candidate": settings.label,
        "settings": asdict(settings),
        "model_id": settings.model_id,
        "n_expected": n_expected,
        "n_attempted": len(items),
        "ok": len(ok),
        "first_ok": sum(1 for i in items if i.first_ok),
        "requests": sum(i.requests for i in items),
        "transient_failures": sum(i.transient_failures for i in items),
        "failures_by_kind": kinds,
        "first_elapsed_s_avg": round(statistics.mean(first), 2) if first else None,
        "first_elapsed_s_max": round(max(first), 2) if first else None,
        "cost_cny_total": round(sum(i.cost_cny for i in items), 6),
        "cost_cny_per_image_ok": round(sum(i.cost_cny for i in items) / len(ok), 6) if ok else None,
        "per_char_per_round": {
            cid: {**pc, "cost_cny": round(pc["cost_cny"] / rounds, 6), "elapsed_s": round(pc["elapsed_s"] / rounds, 3)}
            for cid, pc in per_char.items()
        },
        "sizes": sorted({f"{i.width}x{i.height}" for i in ok}),
        "bytes_avg": round(statistics.mean(i.bytes for i in ok)) if ok else None,
        "items": [{**asdict(i), "seedance_eligible": i.seedance_eligible} for i in items],
    }


def _load_ref(path: Path) -> Ref:
    data = path.read_bytes()
    info = images.image_info(data)
    try:
        shown = str(path.resolve().relative_to(script.REPO_ROOT))
    except ValueError:
        shown = str(path)
    return Ref(shown, info.sha256, info.fmt, data)


def run_costume(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
    out: TextIO | None = None,
    episode: Path = script.SAMPLE_EP01,
    gen_fn: GenerateFn | None = None,
    sleep=time.sleep,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    if not (env.get(seedream.KEY_ENV) or "").strip():
        print(f"缺少 {seedream.KEY_ENV}：在云环境设置或 spikes/poc/.env 中配置", file=out)
        return 2
    doc, sha = load_episode(episode)
    known = [c["id"] for c in doc["characters"]]
    chars = settings.chars or known
    unknown = [c for c in chars if c not in known]
    if unknown:
        print(f"未知角色：{', '.join(unknown)}（可选 {', '.join(known)}）", file=out)
        return 2
    settings.chars = chars
    try:
        billing = seedream.billing_mode(env)
        seedream.check_model(settings.model_id, billing)
    except seedream.ImageError as exc:
        print(str(exc), file=out)
        return 2
    bases: dict[str, Ref] = {}
    if settings.stage == "derive":
        if settings.mode not in MODES:
            print(f"--mode 必须是 {' / '.join(MODES)}", file=out)
            return 2
        missing = [c for c in chars if c not in settings.bases]
        if missing:
            print(f"派生需要每个角色的主图：缺少 {', '.join(missing)}（用 --base 角色=主图路径）", file=out)
            return 2
        for cid in chars:
            try:
                bases[cid] = _load_ref(Path(settings.bases[cid]))
            except (OSError, images.ImageFormatError) as exc:
                print(f"主图不可用 {settings.bases[cid]}：{exc}", file=out)
                return 2
    if settings.rounds < 1 or settings.n < 1:
        print("--rounds 与 --n 至少为 1", file=out)
        return 2
    jobs = plan_jobs(
        doc, settings.stage, chars, n=settings.n, mode=settings.mode, bases=bases, version=settings.prompt_version,
        sizes=SMALL_SIZES if settings.small else SIZES,
    )
    source = {"path": str(episode.relative_to(script.REPO_ROOT)) if episode.is_relative_to(script.REPO_ROOT) else str(episode), "sha256": sha}
    base_info = {cid: {"path": r.path, "sha256": r.sha256} for cid, r in bases.items()}
    args = {"source": source, **asdict(settings), "bases": base_info, "watermark": WATERMARK, "billing": billing}
    with Run("costume", args, base_dir=base_dir, secrets=providers.secret_values(env)) as run:
        runner = Runner(run, settings, env, gen_fn=gen_fn, sleep=sleep)
        print(f"run_id: {run.run_id}  方案: {settings.label}  模型: {settings.model_id}（{billing}）  角色: {', '.join(chars)}  每轮 {len(jobs)} 张", file=out)
        items: list[Item] = []
        aborted = None
        for rnd in range(1, settings.rounds + 1):
            out_dir = run.dir / "images" / f"r{rnd}"
            for job in jobs:
                try:
                    it = runner.item(job, rnd, out_dir)
                except Abort as exc:
                    aborted = str(exc)
                    if exc.item is not None and exc.item.requests:
                        if isinstance(exc, CostLimit):
                            exc.item.error, exc.item.error_kind = f"aborted: {aborted}", "aborted"
                        items.append(exc.item)  # 未完成，按失败计，已产生的费用计入合计
                    print(f"r{rnd} {job.char_id} {job.kind}-{job.label}: 中止：{aborted}", file=out)
                    break
                items.append(it)
                print(
                    f"r{rnd} {job.char_id} {job.kind}-{job.label}: {'成功 ' + str(it.width) + 'x' + str(it.height) if it.ok else '失败 ' + (it.error or '')}"
                    f"  {it.elapsed_s:.1f}s  重试 {it.transient_failures}  ¥{it.cost_cny:.2f}",
                    file=out,
                )
            if aborted:
                break
        summary = summarize(settings, items, len(jobs) * settings.rounds)
        summary.update(
            run_id=run.run_id,
            source=source,
            bases=base_info,
            watermark=WATERMARK,
            output_format=OUTPUT_FORMAT,
            prompt_version=settings.prompt_version,
            billing=billing,
            aborted=aborted,
            spent_cny_total=round(runner.spent, 6),
            price={
                "model": settings.model_id,
                **asdict(pricing.IMAGE_PRICES[(seedream.PROVIDER, settings.model_id)]),
                "tier_pixels": pricing.IMAGE_TIER_PIXELS,
                "source": pricing.IMAGE_PRICE_SOURCE,
                **({"note": pricing.PLAN_COST_NOTE} if billing == "plan" else {}),
            },
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


# ---- 离线：--export / --verify / --cards ----


def export_run(run_dir: Path, dest: Path, rnd: int = 1, out: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    summary_path = run_dir / "summary.json"
    if not summary_path.exists() or not (run_dir / "calls.jsonl").exists():
        print(f"{run_dir} 不是完整的 costume 运行目录（缺少 summary.json 或 calls.jsonl）", file=out)
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
        ext = src.suffix
        rel = Path(it["char_id"]) / f"{it['kind']}-{it['label']}-{it['sha256'][:8]}{ext}"
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / rel)
        entries.append({
            "char_id": it["char_id"], "kind": it["kind"], "label": it["label"], "round": it["round"],
            "file": rel.as_posix(), "sha256": it["sha256"], "bytes": it["bytes"], "width": it["width"], "height": it["height"],
            "reported_size": it["reported_size"], "request_id": it["request_id"], "node_key": it["node_key"],
            "size": it["size"], "section": it["section"], "refs": it["refs"],
            "t2i_original": it["t2i_original"], "seedance_eligible": it["seedance_eligible"], "committed": True,
        })
    manifest = {
        "run_id": summary["run_id"],
        "candidate": summary["candidate"],
        "stage": summary["settings"]["stage"],
        "mode": summary["settings"]["mode"],
        "model_id": summary["model_id"],
        "prompt_version": summary["prompt_version"],
        "watermark": summary["watermark"],
        "output_format": summary["output_format"],
        "billing": summary.get("billing", "payg"),
        "bases": summary["bases"],
        "round": rnd,
        "images": entries,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(summary_path, dest / "run-summary.json")
    shutil.copyfile(run_dir / "calls.jsonl", dest / "run-calls.jsonl")
    print(f"已导出 {len(entries)} 张到 {dest}", file=out)
    return 0


def _read_calls(path: Path, problems: list[str], where: str) -> list[dict[str, Any]]:
    calls = []
    for n, raw in enumerate(path.read_bytes().splitlines(), 1):
        if len(raw) >= MAX_CALL_LINE_BYTES:
            problems.append(f"{where}：run-calls 第 {n} 行 {len(raw)} 字节（≥16 KB，可能含图像数据）")
        if b"b64_json" in raw or b";base64," in raw:
            problems.append(f"{where}：run-calls 第 {n} 行含 base64 图像数据")
        calls.append(json.loads(raw))
    return calls


def verify_dir(d: Path) -> list[str]:
    """复核一个证据目录（含 manifest.json、run-summary.json、run-calls.jsonl）。返回问题列表。"""
    problems: list[str] = []
    where = d.name
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((d / "run-summary.json").read_text(encoding="utf-8"))
    calls = _read_calls(d / "run-calls.jsonl", problems, where)
    if b";base64," in (d / "run-summary.json").read_bytes() or b"b64_json" in (d / "run-summary.json").read_bytes():
        problems.append(f"{where}：run-summary 含 base64 图像数据")
    if manifest["run_id"] != summary["run_id"]:
        problems.append(f"{where}：manifest 与 run-summary 的 run_id 不一致")
    for e in manifest["images"]:
        if e.get("seedance_eligible") and (e.get("refs") or not e.get("t2i_original")):
            problems.append(f"{where}：{e['file']} 有参考图却标为 seedance_eligible")
        if not e.get("committed", True):
            continue
        path = d / e["file"]
        if not path.exists():
            problems.append(f"{where}：缺少 {e['file']}")
            continue
        try:
            info = images.image_info(path.read_bytes())
        except images.ImageFormatError as exc:
            problems.append(f"{where}：{e['file']} 不是有效图像：{exc}")
            continue
        for key, got in (("sha256", info.sha256), ("bytes", info.bytes), ("width", info.width), ("height", info.height)):
            if e[key] != got:
                problems.append(f"{where}：{e['file']} 的 {key} 为 {got}，manifest 记为 {e[key]}")
        if not Path(e["file"]).stem.endswith(info.sha256[:8]):
            problems.append(f"{where}：{e['file']} 文件名中的 sha256 前缀与内容不符")
    listed = {(e["char_id"], e["kind"], e["label"]): e["sha256"] for e in manifest["images"]}
    ok_items = {(i["char_id"], i["kind"], i["label"]): i["sha256"] for i in summary["items"] if i["ok"] and i["round"] == manifest["round"]}
    if listed != ok_items:
        problems.append(f"{where}：manifest 与 run-summary 第 {manifest['round']} 轮成功项不一致（{len(listed)} / {len(ok_items)}）")
    ok_calls = [c for c in calls if c.get("status") == "ok"]
    n_ok = sum(1 for i in summary["items"] if i["ok"])
    if len(ok_calls) != n_ok:
        problems.append(f"{where}：run-calls 成功请求 {len(ok_calls)} 次，run-summary 成功 {n_ok} 张")
    call_shas = {(c.get("extra") or {}).get("sha256") for c in ok_calls}
    for e in manifest["images"]:
        if e["sha256"] not in call_shas:
            problems.append(f"{where}：{e['file']} 在 run-calls 中没有对应的成功请求")
    for c in calls:
        if c.get("provider") != seedream.PROVIDER or c.get("capability") != "image" or c.get("cost_basis") != "estimate":
            problems.append(f"{where}：run-calls 有不符合口径的行（provider / capability / cost_basis）")
            break
    total = round(sum(float(c.get("cost_cny") or 0) for c in calls), 6)
    if abs(total - float(summary["spent_cny_total"])) > 0.01:
        problems.append(f"{where}：run-calls 费用合计 ¥{total} 与 run-summary ¥{summary['spent_cny_total']} 不一致")
    return problems


def run_verify(root: Path, out: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    manifests = sorted(root.rglob("manifest.json"))
    if not manifests:
        print(f"{root} 下没有 manifest.json", file=out)
        return 2
    problems: list[str] = []
    n_images = 0
    total = 0.0
    for m in manifests:
        n_images += len(json.loads(m.read_text(encoding="utf-8"))["images"])
        problems += verify_dir(m.parent)
    for calls_path in sorted(root.rglob("run-calls.jsonl")):
        total += sum(float(json.loads(ln).get("cost_cny") or 0) for ln in calls_path.read_text(encoding="utf-8").splitlines() if ln.strip())
    print(f"复核 {len(manifests)} 个证据目录、{n_images} 张图像；全部 run-calls 费用合计 ¥{total:.2f}", file=out)
    for p in problems:
        print(f"问题：{p}", file=out)
    return 1 if problems else 0


def write_cards(root: Path, episode: Path = script.SAMPLE_EP01, out: TextIO | None = None) -> int:
    """每个角色一张定妆卡 <角色 id>.md：按证据目录分节，图片用 HTML <img> 并排（GitHub 可渲染，不拼图）。"""
    out = sys.stdout if out is None else out
    doc, _ = load_episode(episode)
    # 主图在前，派生在后
    manifests = sorted(root.rglob("manifest.json"), key=lambda m: (not m.parent.name.startswith("main"), m.parent.as_posix()))
    if not manifests:
        print(f"{root} 下没有 manifest.json", file=out)
        return 2
    for c in doc["characters"]:
        lines = [f"# 定妆卡：{c['name']}（`{c['id']}`）", "", f"- 外貌：{c['appearance']}", f"- 服装：{c['costume']}",
                 "- 由 `python3 -m poc costume --cards` 生成；✔ 表示没有参考图的文生图原始产物（`seedance_eligible`），✘ 表示图生图。", ""]
        for m in manifests:
            man = json.loads(m.read_text(encoding="utf-8"))
            entries = [e for e in man["images"] if e["char_id"] == c["id"] and e.get("committed", True)]
            if not entries:
                continue
            rel_dir = m.parent.relative_to(root).as_posix()
            lines += [f"## {rel_dir}（{man['model_id']}，{man['prompt_version']}）", ""]
            cells = []
            for e in entries:
                src = f"{rel_dir}/{e['file']}"
                width = 360 if e["width"] > e["height"] else 180
                cells.append(f"<figure><img src=\"{src}\" width=\"{width}\"><figcaption>{e['kind']}-{e['label']} "
                             f"{'✔' if e['seedance_eligible'] else '✘'}</figcaption></figure>")
            lines += ["<div>", *cells, "</div>", ""]
        (root / f"{c['id']}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        print(f"已写 {root / (c['id'] + '.md')}", file=out)
    return 0


# ---- CLI ----


def _parse_bases(items: list[str] | None, parser) -> dict[str, str]:
    bases: dict[str, str] = {}
    for item in items or []:
        cid, sep, path = item.partition("=")
        if not sep or not cid or not path:
            parser.error(f"--base 格式应为 角色id=主图路径：{item!r}")
        bases[cid] = path
    return bases


def _cmd(args: argparse.Namespace) -> int:
    generation = [o for o, v in (("--stage", args.stage), ("--model", args.model), ("--mode", args.mode), ("--char", args.char),
                                  ("--n", args.n), ("--rounds", args.rounds), ("--base", args.base), ("--name", args.name), ("--small", args.small or None),
                                  ("--prompt-version", args.prompt_version)) if v is not None]
    offline = [o for o, v in (("--export", args.export), ("--verify", args.verify), ("--cards", args.cards)) if v is not None]
    if offline:
        if len(offline) > 1:
            args.parser.error(f"离线模式只能选一个：{', '.join(offline)}")
        if generation:
            args.parser.error(f"离线模式不能与生成参数一起用：{', '.join(generation)}")
        if args.export is not None:
            return export_run(Path(args.export[0]), Path(args.export[1]))
        if args.verify is not None:
            return run_verify(Path(args.verify))
        return write_cards(Path(args.cards))
    if args.stage is None or args.model is None:
        args.parser.error("生成模式需要 --stage 与 --model")
    if args.stage == "derive" and args.mode is None:
        args.parser.error("--stage derive 需要 --mode")
    if args.stage == "main" and (args.mode is not None or args.base is not None):
        args.parser.error("--stage main 不接受 --mode / --base")
    settings = Settings(
        stage=args.stage,
        model=args.model,
        chars=list(args.char or []),
        mode=args.mode,
        n=3 if args.n is None else args.n,
        rounds=1 if args.rounds is None else args.rounds,
        bases=_parse_bases(args.base, args.parser),
        max_cost_cny=args.max_cost_cny,
        name=args.name,
        small=args.small,
        prompt_version=args.prompt_version or PROMPT_VERSION,
    )
    return run_costume(settings)


def add_parser(sub) -> None:
    p = sub.add_parser("costume", help="DramaIR 角色 → 定妆主图、三视图、表情集（P0-06，方舟 Seedream 5.0）")
    p.add_argument("--stage", choices=("main", "derive"), help="main：主图；derive：以主图派生三视图与表情")
    p.add_argument("--model", choices=tuple(seedream.MODELS), help="Seedream 5.0 pro / flash 或 4.0（v4）；ARK_BILLING=plan 时只能 pro")
    p.add_argument("--mode", choices=MODES, help="派生方式：text 文生图 / ref 主图参考图生图 / sheet 设定板")
    p.add_argument("--char", action="append", metavar="角色id", help="只生成这些角色（默认全部），可重复")
    p.add_argument("--n", type=int, default=None, help="main 阶段每个角色的主图张数（默认 3）")
    p.add_argument("--rounds", type=int, default=None, help="轮数（默认 1）")
    p.add_argument("--base", action="append", metavar="角色=主图路径", help="derive 阶段每个角色的主图，可重复")
    p.add_argument("--name", help="方案名（默认 main-<模型> / derive-<方式>-<模型>）")
    p.add_argument("--small", action="store_true", help="用接口允许的最小尺寸（竖 720x1280、横 1280x720、方 960x960），控制入库体积")
    p.add_argument("--prompt-version", choices=PROMPT_VERSIONS, help=f"提示词模板版本（默认 {PROMPT_VERSION}；v2 设定板去掉剧集风格）")
    p.add_argument("--max-cost-cny", type=float, default=15.0, help="本次运行累计估算费用上限（元），预计超出即中止")
    p.add_argument("--export", nargs=2, metavar=("运行目录", "目标目录"), help="离线：把一次运行的第 1 轮整理成入库证据")
    p.add_argument("--verify", metavar="证据根目录", help="离线：复核证据目录（sha256、尺寸、manifest 与 calls 对应、费用）")
    p.add_argument("--cards", metavar="证据根目录", help="离线：按角色生成定妆卡 markdown")
    p.set_defaults(func=_cmd, parser=p)
