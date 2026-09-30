"""shots：1 集 DramaIR v0 剧本 → 重新分镜（P0-04）。

v0 的台词只能挂在镜头上，没有“已分场、未分镜”的形态，所以本步做重新分镜：
代码剥掉输入的镜头结构，渲染成分场剧本视图（只是 Prompt 文本，不是新的数据结构，INV-02）→ LLM 只输出各场的新镜头
→ 代码合并回输入文档的深拷贝，按 <scene_id>_shNN 重排 shot_id → 严格校验 + 守恒检查 + 合理性硬门槛 H1–H4
→ 不通过时带路径回喂，最多修复 max_repairs 轮。生成循环、重试、记账和费用保险复用 script.Generator。

守恒：场的集合与顺序不变；每场台词序列（line_id、speaker、kind、text、delivery 与顺序）与输入完全相同。
硬门槛（T 为目标时长，依据见 docs/reports/p0/P0-04.md）：
- H1 镜头数在 ⌈T / MAX_AVG_SHOT_S⌉ – ⌊T / MIN_AVG_SHOT_S⌋ 之间（T=60 时 12–24）
- H2 单镜 hint_s 在 MIN_SHOT_S – MAX_SHOT_S 之间
- H3 镜头时长合计与 T 的偏差 ≤ TOTAL_TOLERANCE
- H4 每个镜头的台词估算朗读时长 ≤ hint_s（与校验器的严格警告同一口径）
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import string
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO

from poc import llm, pricing, providers, script
from poc.runlog import Run

DEFAULT_PROMPT = "shots.v1"

MIN_AVG_SHOT_S = 2.5
MAX_AVG_SHOT_S = 5.0
MIN_SHOT_S = 1.5
MAX_SHOT_S = 8.0
TOTAL_TOLERANCE = 0.10
EPS = 1e-6

CLOSE_UPS = ("ECU", "CU", "MCU")
ESTABLISHING = ("ELS", "LS", "MLS", "MS")
LONG_SHOT_S = 5.0  # 超过它的镜头要在 P0-08（5 秒片段）拆分或延长，只报告
MIN_SLACK_S = 0.3  # 对白镜头朗读之外的留白（architecture §5.5.1），只报告
IDLE_S = 4.0  # 对白镜头朗读之外空等超过它，只报告

GATES = ("H1", "H2", "H3", "H4")


# ---------------------------------------------------------------- 输入与视图


def shots_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [sh for ep in doc["episodes"] for sc in ep["scenes"] for sh in sc["shots"]]


def scene_lines(scene: dict[str, Any]) -> list[dict[str, Any]]:
    return [line for sh in scene["shots"] for line in sh["dialogue"]]


def _clause(text: str) -> str:
    return text.strip().rstrip("。；;，,.")


def _action(shot: dict[str, Any], names: dict[str, str]) -> str:
    parts = [_clause(shot["description"])]
    for c in shot["characters"]:
        parts.append(f"{names.get(c['character_id'], c['character_id'])}{_clause(c['action'])}（{_clause(c['emotion'])}）")
    if shot["sfx"]:
        parts.append("音效：" + "、".join(_clause(s) for s in shot["sfx"]))
    return "；".join(p for p in parts if p)


def script_view(doc: dict[str, Any]) -> str:
    """把 1 集剥掉镜头结构，渲染成分场剧本文本。

    两句台词之间原镜头的画面描述、人物动作和音效合并成一段动作描写；不出现 shot_id、hint_s、景别和镜头边界。
    台词以原样的 v0 line 对象列出，要求逐字搬运。
    """
    series, ep = doc["series"], doc["episodes"][0]
    names = {c["id"]: c["name"] for c in doc["characters"]}
    out = [
        f"剧名：{series['title']}（{series['genre']}）；视觉风格：{series['visual_style']}；画幅 {series['aspect_ratio']}",
        f"本集：{ep['id']}《{ep['title']}》，目标时长 {ep['target_duration_s']:g} 秒",
        f"本集梗概：{ep['synopsis']}",
        "",
        "## 角色表",
    ]
    for c in doc["characters"]:
        out.append(f"- {c['id']} {c['name']}（{c['gender']}，{c['age']} 岁，{c['role']}）：{c['appearance']}；本集服装：{c['costume']}")
    for i, sc in enumerate(ep["scenes"], 1):
        st = sc["setting"]
        out += [
            "",
            f"## 第 {i} 场 scene_id={sc['scene_id']}",
            f"设定：{st['int_ext']} · {st['location']} · {st['time_of_day']}；{st['description']}",
            f"概要：{sc['summary']}",
            f"情绪基调：{sc['mood']}",
            "",
        ]
        pending: list[str] = []
        for sh in sc["shots"]:
            pending.append(_action(sh, names))
            if sh["dialogue"]:
                out.append("[动作] " + "。".join(pending) + "。")
                pending = []
                for line in sh["dialogue"]:
                    out.append("[台词] " + json.dumps(line, ensure_ascii=False, separators=(",", ":")))
        if pending:
            out.append("[动作] " + "。".join(pending) + "。")
    return "\n".join(out)


def output_schema() -> dict[str, Any]:
    """LLM 输出的 Schema：{"scenes":[{"scene_id","shots":[v0 shot]}]}，镜头定义从 v0 Schema 的 $defs 运行时抽取。"""
    full = script.drama_ir().load_schema("v0")
    defs: dict[str, Any] = {}

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):]
                if name not in defs:
                    defs[name] = full["$defs"][name]
                    collect(defs[name])
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect({"$ref": "#/$defs/shot"})
    return {
        "type": "object",
        "properties": {
            "scenes": {
                "type": "array",
                "description": "按剧本顺序的全部场",
                "items": {
                    "type": "object",
                    "properties": {
                        "scene_id": {"type": "string", "description": "与剧本完全一致"},
                        "shots": {"type": "array", "items": {"$ref": "#/$defs/shot"}, "description": "本场镜头，按播出顺序"},
                    },
                    "required": ["scene_id", "shots"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["scenes"],
        "additionalProperties": False,
        "$defs": {k: defs[k] for k in sorted(defs)},
    }


def target_s(doc: dict[str, Any]) -> float:
    return doc["episodes"][0]["target_duration_s"]


def shot_count_range(target: float) -> tuple[int, int]:
    return math.ceil(target / MAX_AVG_SHOT_S - EPS), math.floor(target / MIN_AVG_SHOT_S + EPS)


def total_range(target: float) -> tuple[float, float]:
    return target * (1 - TOTAL_TOLERANCE), target * (1 + TOTAL_TOLERANCE)


def system_prompt(doc: dict[str, Any], version: str = DEFAULT_PROMPT) -> str:
    checks = script.drama_ir().checks
    target = target_s(doc)
    lo_n, hi_n = shot_count_range(target)
    lo_t, hi_t = total_range(target)
    template = string.Template((script.PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8"))
    return template.substitute(
        scene_count=len(doc["episodes"][0]["scenes"]),
        min_shots=lo_n,
        max_shots=hi_n,
        min_shot_s=f"{MIN_SHOT_S:g}",
        max_shot_s=f"{MAX_SHOT_S:g}",
        target_s=f"{target:g}",
        min_total=f"{lo_t:g}",
        max_total=f"{hi_t:g}",
        tolerance_pct=f"{TOTAL_TOLERANCE * 100:g}",
        cps=f"{checks.CHARS_PER_SECOND:g}",
        schema=json.dumps(output_schema(), ensure_ascii=False, separators=(",", ":")),
    )


def user_prompt(doc: dict[str, Any]) -> str:
    return f"{script_view(doc)}\n\n请输出本集全部场的分镜 JSON 对象。"


# ---------------------------------------------------------------- 守恒、合并与门槛


def conservation_issues(source: dict[str, Any], out: dict[str, Any]) -> list[str]:
    """LLM 输出（已通过结构校验）与输入的场、台词是否守恒。路径指向 LLM 输出。"""
    src_scenes = source["episodes"][0]["scenes"]
    src_ids = [sc["scene_id"] for sc in src_scenes]
    out_ids = [sc["scene_id"] for sc in out["scenes"]]
    if out_ids != src_ids:
        return [f"$.scenes: 场必须与剧本完全一致、按顺序：应为 {src_ids}，实际为 {out_ids}"]

    home = {line["line_id"]: sc["scene_id"] for sc in src_scenes for line in scene_lines(sc)}
    issues = []
    for i, (src, got) in enumerate(zip(src_scenes, out["scenes"])):
        spath = f"$.scenes[{i}]"
        if not got["shots"]:
            issues.append(f"{spath}.shots: 每场至少需要 1 个镜头")
        want = scene_lines(src)
        want_by_id = {line["line_id"]: line for line in want}
        placed = [
            (f"{spath}.shots[{h}].dialogue[{d}]", line)
            for h, sh in enumerate(got["shots"])
            for d, line in enumerate(sh["dialogue"])
        ]
        seen: Counter[str] = Counter()
        for path, line in placed:
            lid = line["line_id"]
            seen[lid] += 1
            if lid not in want_by_id:
                where = f"属于场 {home[lid]}，不能挪到本场" if lid in home else "剧本中没有这句台词，不能新增"
                issues.append(f"{path}: 台词 {lid} {where}")
                continue
            if seen[lid] == 2:
                issues.append(f"{path}: 台词 {lid} 重复出现，每句台词只能挂载一次")
            for key in ("speaker", "kind", "text", "delivery"):
                if line[key] != want_by_id[lid][key]:
                    issues.append(
                        f"{path}.{key}: 必须与剧本逐字一致，应为 {json.dumps(want_by_id[lid][key], ensure_ascii=False)}"
                    )
        missing = [lid for lid in want_by_id if lid not in seen]
        if missing:
            issues.append(f"{spath}: 缺少台词 {missing}，剧本中每句台词都必须挂到本场的某个镜头上")
        got_order = [line["line_id"] for _, line in placed if line["line_id"] in want_by_id]
        want_order = [line["line_id"] for line in want]
        if not missing and len(got_order) == len(want_order) and got_order != want_order:
            issues.append(f"{spath}: 台词顺序与剧本不一致：应为 {want_order}，实际为 {got_order}")
    return issues


def merge(source: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 输出的镜头合并进输入文档的深拷贝，shot_id 按 <scene_id>_shNN 重排。要求场已守恒。"""
    doc = copy.deepcopy(source)
    for sc, got in zip(doc["episodes"][0]["scenes"], out["scenes"]):
        shots = copy.deepcopy(got["shots"])
        for k, sh in enumerate(shots, 1):
            sh["shot_id"] = f"{sc['scene_id']}_sh{k:02d}"
        sc["shots"] = shots
    return doc


def gate_issues(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """合理性硬门槛 H1–H4，返回 [(门槛, "路径: 说明")]；路径指向 DramaIR 文档。只看第 1 集。"""
    checks = script.drama_ir().checks
    ep = doc["episodes"][0]
    target = ep["target_duration_s"]
    shots = [(f"$.episodes[0].scenes[{s}].shots[{h}]", sh) for s, sc in enumerate(ep["scenes"]) for h, sh in enumerate(sc["shots"])]
    issues: list[tuple[str, str]] = []
    lo_n, hi_n = shot_count_range(target)
    if not lo_n <= len(shots) <= hi_n:
        issues.append(("H1", f"$.episodes[0]: 镜头共 {len(shots)} 个，应在 {lo_n}–{hi_n} 个之间（目标 {target:g} 秒，平均单镜 {MIN_AVG_SHOT_S:g}–{MAX_AVG_SHOT_S:g} 秒）"))
    for path, sh in shots:
        hint = sh["duration"]["hint_s"]
        if not MIN_SHOT_S - EPS <= hint <= MAX_SHOT_S + EPS:
            issues.append(("H2", f"{path}.duration.hint_s: 单镜时长 {hint:g} 秒，应在 {MIN_SHOT_S:g}–{MAX_SHOT_S:g} 秒之间"))
    total = sum(sh["duration"]["hint_s"] for _, sh in shots)
    lo_t, hi_t = total_range(target)
    if not lo_t - EPS <= total <= hi_t + EPS:
        issues.append(("H3", f"$.episodes[0]: 镜头时长合计 {total:g} 秒，应在 {lo_t:g}–{hi_t:g} 秒之间（目标 {target:g} 秒 ±{TOTAL_TOLERANCE:.0%}）"))
    for path, sh in shots:
        speech = sum(checks.speech_seconds(line) for line in sh["dialogue"])
        if speech > sh["duration"]["hint_s"] + EPS:
            issues.append(("H4", f"{path}.duration.hint_s: 台词估算朗读 {speech:.2f} 秒，超过镜头时长 {sh['duration']['hint_s']:g} 秒"))
    return issues


# ---------------------------------------------------------------- 指标


def _groups(doc: dict[str, Any]) -> list[tuple[str, ...]]:
    return [tuple(line["line_id"] for line in sh["dialogue"]) for sh in shots_of(doc) if sh["dialogue"]]


def _dist(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items(), key=lambda kv: (-kv[1], kv[0])))


def metrics(doc: dict[str, Any], source: dict[str, Any] | None = None) -> dict[str, Any]:
    """镜头数量与时长合理性指标（只看第 1 集）。source 给出时附带与原分镜的相似度。"""
    checks = script.drama_ir().checks
    ep = doc["episodes"][0]
    shots = shots_of(doc)
    n = len(shots)
    hints = [sh["duration"]["hint_s"] for sh in shots]
    total = sum(hints)
    dialog = [(sh["duration"]["hint_s"], sum(checks.speech_seconds(line) for line in sh["dialogue"])) for sh in shots if sh["dialogue"]]
    fills = [sp / h for h, sp in dialog]
    slacks = [h - sp for h, sp in dialog]
    sizes = [sh["framing"]["shot_size"] for sh in shots]
    firsts = [sc["shots"][0]["framing"]["shot_size"] for sc in ep["scenes"] if sc["shots"]]
    gates = gate_issues(doc)
    lo_n, hi_n = shot_count_range(ep["target_duration_s"])
    m: dict[str, Any] = {
        "target_s": ep["target_duration_s"],
        "scenes": len(ep["scenes"]),
        "shots": n,
        "shot_range": [lo_n, hi_n],
        "total_s": round(total, 3),
        "total_dev": round((total - ep["target_duration_s"]) / ep["target_duration_s"], 4),
        "shot_s_avg": round(total / n, 3) if n else None,
        "shot_s_min": min(hints) if n else None,
        "shot_s_max": max(hints) if n else None,
        "shots_over_5s": sum(h > LONG_SHOT_S + EPS for h in hints),
        "shots_over_5s_ratio": round(sum(h > LONG_SHOT_S + EPS for h in hints) / n, 3) if n else None,
        "lines": sum(len(sh["dialogue"]) for sh in shots),
        "dialogue_shots": len(dialog),
        "fill_avg": round(sum(fills) / len(fills), 3) if fills else None,
        "fill_max": round(max(fills), 3) if fills else None,
        "slack_min_s": round(min(slacks), 3) if slacks else None,
        "slack_ok_ratio": round(sum(s >= MIN_SLACK_S - EPS for s in slacks) / len(slacks), 3) if slacks else None,
        "idle_shots": sum(s > IDLE_S + EPS for s in slacks),
        "max_lines_per_shot": max((len(sh["dialogue"]) for sh in shots), default=0),
        "multi_speaker_shots": sum(len({line["speaker"] for line in sh["dialogue"]}) > 1 for sh in shots),
        "empty_shots": sum(not sh["characters"] for sh in shots),
        "lipsync_shots": sum(any(line["kind"] == "dialogue" for line in sh["dialogue"]) for sh in shots),
        "shot_sizes": _dist(sizes),
        "shot_size_kinds": len(set(sizes)),
        "close_up_ratio": round(sum(s in CLOSE_UPS for s in sizes) / n, 3) if n else None,
        "establishing_first": sum(s in ESTABLISHING for s in firsts),
        "movements": _dist([sh["framing"]["movement"] for sh in shots]),
        "static_ratio": round(sum(sh["framing"]["movement"] == "static" for sh in shots) / n, 3) if n else None,
        "angles": _dist([sh["framing"]["angle"] for sh in shots]),
        "gates": {g: not any(k == g for k, _ in gates) for g in GATES},
        "gate_issues": [f"{k} {msg}" for k, msg in gates],
    }
    if source is not None:
        new, old = _groups(doc), _groups(source)
        old_set = set(old)
        m["vs_source"] = {
            "source_shots": len(shots_of(source)),
            "shot_count_delta": n - len(shots_of(source)),
            "same_line_groups_ratio": round(sum(g in old_set for g in new) / len(new), 3) if new else None,
            "identical_line_grouping": new == old,
        }
    return m


def metrics_line(m: dict[str, Any]) -> str:
    gates = " ".join(f"{g}{'✓' if ok else '✗'}" for g, ok in m["gates"].items())
    return (
        f"镜头 {m['shots']}（{m['shot_range'][0]}–{m['shot_range'][1]}）  时长 {m['total_s']:g}/{m['target_s']:g}s  "
        f"单镜 {m['shot_s_min']:g}–{m['shot_s_max']:g}s 平均 {m['shot_s_avg']:g}s  >5s {m['shots_over_5s']}  "
        f"景别 {m['shot_size_kinds']} 种 近景特写 {m['close_up_ratio']:.0%}  留白≥{MIN_SLACK_S:g}s {m['slack_ok_ratio']:.0%}  {gates}"
    )


# ---------------------------------------------------------------- 生成


def _rewrite_path(issue: str) -> str:
    """校验器报在合并后文档上的路径，改写成 LLM 输出的路径。"""
    for prefix, repl in (("$.episodes[0].scenes", "$.scenes"), ("$.episodes[0]", "$（全集）")):
        if issue.startswith(prefix):
            return repl + issue[len(prefix):]
    return issue


@dataclass
class ShotSettings(script.Settings):
    prompt_version: str = DEFAULT_PROMPT
    target_s: int | None = None  # 取自输入文档，不可配置


class ShotGenerator(script.Generator):
    def __init__(self, run: Run, settings: ShotSettings, source: dict[str, Any], chat_fn, env, sleep=time.sleep):
        self.source = source
        self.schema = output_schema()
        super().__init__(run, settings, source["series"]["logline"], chat_fn, env, sleep=sleep)

    def build_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_prompt(self.source, self.s.prompt_version)},
            {"role": "user", "content": user_prompt(self.source)},
        ]

    def _check(self, text: str, attempt: script.Attempt) -> tuple[list[str], Any]:
        ir = script.drama_ir()
        try:
            out = json.loads(text, parse_constant=script._reject_constant)
        except ValueError as exc:
            attempt.outcome, attempt.detail = "not_json", f"{type(exc).__name__}: {exc}"
            return [f"$: 输出不是合法 JSON（{exc}）；只输出一个 JSON 对象"], None
        issues = [f"错误 {i}" for i in ir.schema.structural_issues(out, self.schema)]
        if not issues:
            issues = [f"错误 {i}" for i in conservation_issues(self.source, out)]
        doc = None
        warnings = 0
        if not issues:
            doc = merge(self.source, out)
            report = ir.validate(doc)
            warnings = len(report.warnings)
            issues = [f"错误 {_rewrite_path(str(i))}" for i in report.errors] + [f"警告 {_rewrite_path(str(i))}" for i in report.warnings]
            if not report.errors:
                # H4 与校验器的严格警告同一口径，已在上面报过，不重复回喂
                issues += [f"错误 {_rewrite_path(msg)}" for g, msg in gate_issues(doc) if g != "H4"]
        attempt.errors = sum(i.startswith("错误") for i in issues)
        attempt.warnings = warnings
        attempt.outcome = "pass" if not issues else "invalid"
        return issues, doc


def repo_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(script.REPO_ROOT))
    except ValueError:
        return str(path)


def load_input(path: Path) -> tuple[dict[str, Any] | None, str]:
    """读取输入剧本；要求通过严格校验且只有 1 集。返回 (文档, 出错说明)。"""
    try:
        raw = path.read_bytes()
        doc = json.loads(raw, parse_constant=script._reject_constant)
    except (OSError, ValueError) as exc:
        return None, f"{path}: 读取失败：{exc}"
    report = script.drama_ir().validate(doc)
    if not report.ok(strict=True):
        return None, f"{path}: 输入没有通过严格校验（{len(report.errors)} 个错误，{len(report.warnings)} 个警告）"
    if len(doc["episodes"]) != 1:
        return None, f"{path}: 输入应只有 1 集，实际 {len(doc['episodes'])} 集"
    return doc, hashlib.sha256(raw).hexdigest()


def run_shots(
    settings: ShotSettings,
    inputs: list[Path] | None = None,
    n: int = 5,
    env: Mapping[str, str] | None = None,
    chat_fn: script.ChatFn | None = None,
    balance_fn: Callable[[str], float | None] | None = None,
    base_dir: Path | None = None,
    out: TextIO | None = None,
    sleep=time.sleep,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    endpoint = llm.ENDPOINTS.get(settings.provider)
    if endpoint is None:
        print(f"未知的供应商：{settings.provider}；可选：{', '.join(llm.ENDPOINTS)}", file=out)
        return 2
    if not (env.get(endpoint.key_env) or "").strip():
        print(f"缺少 {endpoint.key_env}：在云环境设置或 spikes/poc/.env 中配置", file=out)
        return 2
    if n < 1 or settings.max_repairs < 0:
        print("--n 至少为 1，--max-repairs 不能为负", file=out)
        return 2
    paths = inputs or [script.SAMPLE_EP01]
    sources = []
    for p in paths:
        doc, info = load_input(p)
        if doc is None:
            print(info, file=out)
            return 2
        sources.append((p, doc, {"path": repo_rel(p), "sha256": info}))
    balance_fn = balance_fn or (lambda p: llm.balance_cny(p, env))

    args = {"n": n, "inputs": [s[2] for s in sources], **asdict(settings)}
    with Run("shots", args, base_dir=base_dir, secrets=providers.secret_values(env)) as run:
        print(f"run_id: {run.run_id}  模型: {settings.provider}/{settings.model}  Prompt: {settings.prompt_version}", file=out)
        balance_before = balance_fn(settings.provider)
        samples: list[script.Sample] = []
        sample_meta: dict[str, Any] = {}
        prompts = []
        aborted = None
        spent = 0.0
        samples_dir = run.dir / "samples"
        for i, (path, source, info) in enumerate(sources, 1):
            gen = ShotGenerator(run, settings, source, chat_fn or llm.chat, env, sleep=sleep)
            gen.spent = spent  # 费用保险按整次运行累计
            prompts.append({"input": info, "messages": gen.base_messages})
            print(f"输入 {info['path']}（镜头 {len(shots_of(source))}，目标 {target_s(source):g}s）", file=out)
            for k in range(1, n + 1):
                name = f"s{k:02d}" if len(sources) == 1 else f"in{i:02d}_s{k:02d}"
                try:
                    s = gen.sample(name, samples_dir)
                except script.CostLimit as exc:
                    aborted = str(exc)
                    if exc.sample is not None and exc.sample.attempts:
                        samples.append(exc.sample)
                        sample_meta[name] = {"input": info["path"]}
                    print(f"{name}: 中止：{aborted}", file=out)
                    break
                samples.append(s)
                meta: dict[str, Any] = {"input": info["path"]}
                line = ""
                if s.passed:
                    final = json.loads((samples_dir / name / "final.json").read_text(encoding="utf-8"))
                    m = metrics(final, source)
                    run.write_json(f"samples/{name}/metrics.json", m)
                    meta["metrics"] = m
                    line = metrics_line(m)
                sample_meta[name] = meta
                trail = " → ".join(a.outcome for a in s.attempts)
                print(
                    f"{name}: {'通过' if s.passed else '失败'}  修复 {s.repairs} 轮 [{trail}]  ¥{s.cost_cny:.4f}  {s.elapsed_s:.0f}s  {line}",
                    file=out,
                )
            spent = gen.spent
            if aborted:
                break
        run.write_json("prompt.json", {"prompt_version": settings.prompt_version, "settings": asdict(settings), "inputs": prompts})
        balance_after = balance_fn(settings.provider)
        summary = script.summarize(samples, settings)
        for s in summary["samples"]:
            s.update(sample_meta.get(s["sample"], {}))
        summary.update(
            run_id=run.run_id,
            n_requested=n,
            inputs=[s[2] for s in sources],
            spent_cny_total=round(spent, 4),
            aborted=aborted,
            price=asdict(pricing.price_for(settings.provider, settings.model)),
            balance_before_cny=balance_before,
            balance_after_cny=balance_after,
            balance_delta_cny=(
                round(balance_before - balance_after, 2) if balance_before is not None and balance_after is not None else None
            ),
        )
        run.write_json("summary.json", summary)
        expected = n * len(sources)
        ok = aborted is None and summary["passed"] == expected
        print(
            f"\n通过 {summary['passed']}/{expected}（首次通过 {summary['first_pass']}），估算费用 ¥{summary['cost_cny_total']:.4f}"
            + (f"，余额差 ¥{summary['balance_delta_cny']:.2f}" if summary["balance_delta_cny"] is not None else ""),
            file=out,
        )
        print(f"输出：{run.dir}", file=out)
        if not ok:
            run.meta["result"] = "failed"
    return 0 if ok else 1


def run_metrics(paths: list[Path], source: Path | None = None, as_json: bool = False, out: TextIO | None = None) -> int:
    """离线指标：不调用模型。全部文件通过严格校验且 H1–H4 全部通过时退出码为 0。"""
    out = sys.stdout if out is None else out
    src_doc = None
    if source is not None:
        src_doc, info = load_input(source)
        if src_doc is None:
            print(info, file=out)
            return 2
    results = []
    ok = True
    for p in paths:
        try:
            doc = json.loads(p.read_bytes(), parse_constant=script._reject_constant)
        except (OSError, ValueError) as exc:
            print(f"{p}: 读取失败：{exc}", file=out)
            ok = False
            continue
        report = script.drama_ir().validate(doc)
        if report.errors or len(doc["episodes"]) != 1:
            print(f"{p}: 无法计算指标（{len(report.errors)} 个校验错误，{len(doc.get('episodes') or [])} 集）", file=out)
            ok = False
            continue
        m = metrics(doc, src_doc)
        m["strict_ok"] = report.ok(strict=True)
        ok = ok and m["strict_ok"] and all(m["gates"].values())
        results.append({"path": repo_rel(p), **m})
        if not as_json:
            print(f"{repo_rel(p)}: 严格校验{'通过' if m['strict_ok'] else '失败'}  {metrics_line(m)}", file=out)
            for issue in m["gate_issues"]:
                print(f"  {issue}", file=out)
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2), file=out)
    return 0 if ok else 1


def _cmd(args: argparse.Namespace) -> int:
    if args.metrics:
        return run_metrics([Path(p) for p in args.metrics], Path(args.source) if args.source else None, args.json)
    settings = ShotSettings(
        provider=args.provider,
        model=args.model,
        prompt_version=args.prompt_version,
        max_repairs=args.max_repairs,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        thinking=args.thinking,
        reasoning_effort=args.reasoning_effort,
        max_cost_cny=args.max_cost_cny,
    )
    return run_shots(settings, inputs=[Path(p) for p in args.input] if args.input else None, n=args.n)


def add_parser(sub) -> None:
    p = sub.add_parser("shots", help="1 集 DramaIR v0 剧本 → 重新分镜（P0-04）")
    p.add_argument("--input", nargs="+", help="输入剧本（DramaIR v0 JSON，1 集）；默认标准样例 ep01")
    p.add_argument("--n", type=int, default=5, help="每个输入顺序生成的样本数（默认 5）")
    p.add_argument("--metrics", nargs="+", metavar="JSON", help="离线模式：只计算这些文档的分镜指标与 H1–H4，不调用模型")
    p.add_argument("--source", help="与 --metrics 一起用：原分镜文档，用于计算相似度")
    p.add_argument("--json", action="store_true", help="与 --metrics 一起用：输出完整 JSON")
    script.add_llm_options(p, DEFAULT_PROMPT)
    p.set_defaults(func=_cmd)
