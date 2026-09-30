"""script：一句话梗概 → 1 集 DramaIR v0 剧本（P0-03）。

每份样本：生成 → json.loads → dramio_drama_ir.validate（strict：警告也算失败）+ 本步附加检查
→ 不通过时把上一版输出和问题清单（带 JSON 路径）回喂，要求重写完整 JSON，最多 max_repairs 轮。
N 份样本在一次运行内顺序生成，不挑选、不丢弃；全部通过时退出码为 0。

网络错误、429、5xx 属于瞬时故障，同一次尝试内最多重试 TRANSIENT_RETRIES 次，不占用修复轮数（每次请求都记账）。
截断、空输出、非 JSON、校验失败都算一次失败的尝试。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import string
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from poc import config, llm, pricing, providers
from poc.runlog import Run

REPO_ROOT = config.PROJECT_DIR.parent.parent
DRAMA_IR_PYTHON = REPO_ROOT / "packages" / "drama-ir" / "python"
SAMPLE_EP01 = REPO_ROOT / "packages" / "drama-ir" / "examples" / "v0" / "ep01.json"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

DEFAULT_PROMPT = "script.v1"
DEFAULT_MODEL = "deepseek-flash"
MAX_ISSUES_IN_FEEDBACK = 30
TRANSIENT_RETRIES = 2


def drama_ir():
    """导入 dramio_drama_ir；从 spikes/poc 直接运行时自动把 packages/drama-ir/python 加入 sys.path。"""
    try:
        import dramio_drama_ir
    except ImportError:
        sys.path.insert(0, str(DRAMA_IR_PYTHON))
        import dramio_drama_ir
    return dramio_drama_ir


def drama_ir_render():
    drama_ir()
    import dramio_drama_ir.render

    return dramio_drama_ir.render


def default_logline() -> tuple[str, str]:
    """标准样例 ep01 的梗概（D-002 固定输入）及样例文件的 sha256。"""
    raw = SAMPLE_EP01.read_bytes()
    return json.loads(raw)["series"]["logline"], hashlib.sha256(raw).hexdigest()


def llm_schema() -> dict[str, Any]:
    """交给 LLM 的 Schema：去掉根上的 $schema、$id（不改 Schema 文件本身）。"""
    schema = dict(drama_ir().load_schema("v0"))
    schema.pop("$schema", None)
    schema.pop("$id", None)
    return schema


def system_prompt(target_s: int, version: str = DEFAULT_PROMPT) -> str:
    """渲染 Prompt 模板；阈值直接取自校验器常量，避免与校验规则漂移。"""
    checks = drama_ir().checks
    template = string.Template((PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8"))
    tol = checks.DURATION_TOLERANCE
    return template.substitute(
        target_s=target_s,
        min_total=f"{target_s * (1 - tol):g}",
        max_total=f"{target_s * (1 + tol):g}",
        tolerance_pct=f"{tol * 100:g}",
        max_shot_s=f"{checks.MAX_SHOT_S:g}",
        cps=f"{checks.CHARS_PER_SECOND:g}",
        speed_min=f"{checks.SPEED_RANGE[0]:g}",
        speed_max=f"{checks.SPEED_RANGE[1]:g}",
        id_pattern=checks.ID_PATTERN.pattern,
        schema=json.dumps(llm_schema(), ensure_ascii=False, separators=(",", ":")),
    )


def user_prompt(logline: str) -> str:
    return f"一句话梗概：{logline}\n\n请输出第 1 集剧本的 DramaIR v0 JSON 对象。"


def repair_prompt(issues: list[str]) -> str:
    shown = issues[:MAX_ISSUES_IN_FEEDBACK]
    more = f"\n（另有 {len(issues) - len(shown)} 条未列出）" if len(issues) > len(shown) else ""
    lines = "\n".join(f"- {i}" for i in shown)
    return (
        f"上面的 JSON 没有通过校验，共 {len(issues)} 个问题：\n{lines}{more}\n\n"
        "请逐条修正，同时不要引入新问题（改镜头时长时重新核对全集时长合计和每个镜头的台词朗读时长）。"
        "保持剧情不变，输出修正后的**完整** JSON 对象。"
    )


def spec_issues(doc: dict[str, Any], logline: str) -> list[str]:
    """校验器之外的本步要求：只生成 1 集，logline 原样使用输入。只在结构校验通过后调用。"""
    issues = []
    if len(doc["episodes"]) != 1:
        issues.append(f"$.episodes: 应只有 1 集，实际 {len(doc['episodes'])} 集")
    if doc["series"]["logline"] != logline:
        issues.append("$.series.logline: 必须原样使用用户给出的梗概")
    return issues


def doc_stats(doc: dict[str, Any]) -> dict[str, Any]:
    shots = [s for ep in doc["episodes"] for sc in ep["scenes"] for s in sc["shots"]]
    return {
        "characters": len(doc["characters"]),
        "episodes": len(doc["episodes"]),
        "scenes": sum(len(ep["scenes"]) for ep in doc["episodes"]),
        "shots": len(shots),
        "lines": sum(len(s["dialogue"]) for s in shots),
        "duration_s": round(sum(s["duration"]["hint_s"] for s in shots), 2),
    }


@dataclass
class Attempt:
    attempt: int  # 0 = 首次生成，1.. = 修复轮
    outcome: str  # pass / invalid / not_json / truncated / empty / http / network / bad_response
    errors: int = 0
    warnings: int = 0
    requests: int = 0  # 含瞬时故障重试
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_cny: float = 0.0
    elapsed_s: float = 0.0
    detail: str = ""


@dataclass
class Sample:
    sample: str
    passed: bool = False
    first_pass: bool = False
    repairs: int = 0
    attempts: list[Attempt] = field(default_factory=list)
    stats: dict[str, Any] | None = None
    cost_cny: float = 0.0
    elapsed_s: float = 0.0


class CostLimit(Exception):
    sample: "Sample | None" = None  # 中止时未完成的样本


ChatFn = Callable[..., llm.ChatResult]


@dataclass
class Settings:
    provider: str = "deepseek"
    model: str = DEFAULT_MODEL
    prompt_version: str = DEFAULT_PROMPT
    target_s: int = 60
    max_repairs: int = 2
    temperature: float | None = None
    max_tokens: int = 32000
    thinking: str = "default"  # default / enabled / disabled
    reasoning_effort: str | None = None
    max_cost_cny: float = 20.0

    def extra_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if self.thinking != "default":
            body["thinking"] = {"type": self.thinking}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        return body


class Generator:
    def __init__(self, run: Run, settings: Settings, logline: str, chat_fn: ChatFn, env: Mapping[str, str], sleep=time.sleep):
        self.run = run
        self.s = settings
        self.logline = logline
        self.chat_fn = chat_fn
        self.env = env
        self.sleep = sleep
        self.spent = 0.0
        self.base_messages = [
            {"role": "system", "content": system_prompt(settings.target_s, settings.prompt_version)},
            {"role": "user", "content": user_prompt(logline)},
        ]

    def _request(self, messages, sample: str, attempt: Attempt) -> llm.ChatResult:
        """一次尝试：瞬时故障（网络、429、5xx）重试；其余 LLMError 原样抛出。每次请求都记账。"""
        for retry in range(TRANSIENT_RETRIES + 1):
            if self.spent >= self.s.max_cost_cny:
                raise CostLimit(f"累计估算费用 ¥{self.spent:.4f} 已达上限 ¥{self.s.max_cost_cny:g}")
            attempt.requests += 1
            try:
                with self.run.call(provider=self.s.provider, capability="llm", model=self.s.model) as call:
                    call.cost_basis = "estimate"
                    call.extra = {"sample": sample, "attempt": attempt.attempt, "retry": retry, "prompt_version": self.s.prompt_version}
                    try:
                        res = self.chat_fn(
                            self.s.provider, self.s.model, messages,
                            json_mode=True, max_tokens=self.s.max_tokens, temperature=self.s.temperature,
                            extra_body=self.s.extra_body(), env=self.env,
                        )
                    except llm.LLMError as exc:
                        self._account(call, attempt, exc.usage)
                        call.extra["error_kind"] = exc.kind
                        raise
                    self._account(call, attempt, res.usage)
                    call.request_id = res.request_id
                    call.extra.update(finish_reason=res.finish_reason, reasoning_chars=res.reasoning_chars)
                    return res
            except llm.LLMError as exc:
                transient = exc.kind == "network" or (exc.kind == "http" and (exc.http_status == 429 or (exc.http_status or 0) >= 500))
                if not transient or retry == TRANSIENT_RETRIES:
                    raise
                self.sleep(2 ** (retry + 1))
        raise AssertionError("unreachable")

    def _account(self, call, attempt: Attempt, usage: dict | None) -> None:
        cost = pricing.estimate_cny(self.s.provider, self.s.model, usage)
        call.cost_cny = cost
        call.usage = usage
        self.spent += cost
        attempt.cost_cny += cost
        if usage:
            attempt.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            attempt.completion_tokens += int(usage.get("completion_tokens") or 0)
            attempt.reasoning_tokens += int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)

    def sample(self, name: str, out_dir: Path) -> Sample:
        dir_ = out_dir / name
        dir_.mkdir(parents=True, exist_ok=True)
        result = Sample(sample=name)
        messages = list(self.base_messages)
        t_sample = time.monotonic()
        try:
            self._attempts(result, messages, dir_)
        except CostLimit as exc:
            # 中止时把这份未完成的样本也交给调用方，它已花的费用要计入 summary
            last = result.attempts[-1] if result.attempts else None
            if last is not None and not last.outcome:
                last.outcome, last.detail = "aborted", str(exc)
            exc.sample = self._finish(result, t_sample)
            raise
        return self._finish(result, t_sample)

    def _finish(self, result: Sample, t_sample: float) -> Sample:
        if not result.passed:
            result.repairs = max(len(result.attempts) - 1, 0)
        result.cost_cny = round(sum(a.cost_cny for a in result.attempts), 6)
        result.elapsed_s = round(time.monotonic() - t_sample, 1)
        return result

    def _attempts(self, result: Sample, messages: list[dict[str, str]], dir_: Path) -> None:
        name = result.sample
        for k in range(self.s.max_repairs + 1):
            attempt = Attempt(attempt=k, outcome="")
            result.attempts.append(attempt)
            t0 = time.monotonic()
            try:
                res = self._request(messages, name, attempt)
            except llm.LLMError as exc:
                attempt.outcome, attempt.detail = exc.kind, str(exc)
                attempt.elapsed_s = round(time.monotonic() - t0, 1)
                # 没有可修复的输出：下一轮从头生成
                messages = list(self.base_messages)
                continue
            attempt.elapsed_s = round(time.monotonic() - t0, 1)
            (dir_ / f"attempt{k}.raw.txt").write_text(res.text, encoding="utf-8")
            issues, doc = self._check(res.text, attempt)
            (dir_ / f"attempt{k}.report.json").write_text(
                json.dumps({"outcome": attempt.outcome, "issues": issues}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            if attempt.outcome == "pass":
                result.passed = True
                result.first_pass = k == 0
                result.repairs = k
                result.stats = doc_stats(doc)
                (dir_ / "final.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                (dir_ / "final.md").write_text(drama_ir_render().render_markdown(doc), encoding="utf-8")
                break
            messages = self.base_messages + [
                {"role": "assistant", "content": res.text},
                {"role": "user", "content": repair_prompt(issues)},
            ]

    def _check(self, text: str, attempt: Attempt) -> tuple[list[str], Any]:
        try:
            doc = json.loads(text, parse_constant=_reject_constant)
        except ValueError as exc:
            attempt.outcome, attempt.detail = "not_json", f"{type(exc).__name__}: {exc}"
            return [f"$: 输出不是合法 JSON（{exc}）；只输出一个 JSON 对象"], None
        report = drama_ir().validate(doc)
        issues = [f"错误 {i}" for i in report.errors] + [f"警告 {i}" for i in report.warnings]
        if not report.errors:
            issues += [f"错误 {i}" for i in spec_issues(doc, self.logline)]
        attempt.errors = sum(i.startswith("错误") for i in issues)
        attempt.warnings = len(report.warnings)
        attempt.outcome = "pass" if not issues else "invalid"
        return issues, doc


def _reject_constant(name: str):
    raise ValueError(f"非法的 JSON 常量 {name}")


def summarize(samples: list[Sample], settings: Settings) -> dict[str, Any]:
    n = len(samples)
    attempts = [a for s in samples for a in s.attempts]
    failures: dict[str, int] = {}
    for a in attempts:
        if a.outcome != "pass":
            failures[a.outcome] = failures.get(a.outcome, 0) + 1
    costs = [s.cost_cny for s in samples]
    times = [s.elapsed_s for s in samples]
    return {
        "settings": asdict(settings),
        "n": n,
        "passed": sum(s.passed for s in samples),
        "first_pass": sum(s.first_pass for s in samples),
        "first_pass_rate": round(sum(s.first_pass for s in samples) / n, 3) if n else None,
        "pass_rate": round(sum(s.passed for s in samples) / n, 3) if n else None,
        "repairs": [s.repairs for s in samples],
        "attempts_total": len(attempts),
        "requests_total": sum(a.requests for a in attempts),
        "failed_attempts_by_kind": failures,
        "prompt_tokens_total": sum(a.prompt_tokens for a in attempts),
        "completion_tokens_total": sum(a.completion_tokens for a in attempts),
        "reasoning_tokens_total": sum(a.reasoning_tokens for a in attempts),
        "cost_cny_total": round(sum(costs), 4),
        "cost_cny_per_sample_avg": round(sum(costs) / n, 4) if n else None,
        "cost_cny_per_sample_max": round(max(costs), 4) if n else None,
        "elapsed_s_per_sample_avg": round(sum(times) / n, 1) if n else None,
        "elapsed_s_per_sample_max": round(max(times), 1) if n else None,
        "samples": [asdict(s) for s in samples],
    }


def run_script(
    settings: Settings,
    n: int = 5,
    logline: str | None = None,
    env: Mapping[str, str] | None = None,
    chat_fn: ChatFn | None = None,
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

    source = {"type": "arg"}
    if logline is None:
        logline, sha = default_logline()
        source = {"type": "sample", "path": "packages/drama-ir/examples/v0/ep01.json", "sha256": sha}
    balance_fn = balance_fn or (lambda p: llm.balance_cny(p, env))

    args = {"n": n, "logline": logline, "logline_source": source, **asdict(settings)}
    with Run("script", args, base_dir=base_dir, secrets=providers.secret_values(env)) as run:
        gen = Generator(run, settings, logline, chat_fn or llm.chat, env, sleep=sleep)
        run.write_json(
            "prompt.json",
            {"prompt_version": settings.prompt_version, "messages": gen.base_messages, "settings": asdict(settings)},
        )
        balance_before = balance_fn(settings.provider)
        print(f"run_id: {run.run_id}  模型: {settings.provider}/{settings.model}  Prompt: {settings.prompt_version}", file=out)
        print(f"梗概：{logline}", file=out)
        samples: list[Sample] = []
        aborted = None
        samples_dir = run.dir / "samples"
        for i in range(1, n + 1):
            name = f"s{i:02d}"
            try:
                s = gen.sample(name, samples_dir)
            except CostLimit as exc:
                aborted = str(exc)
                if exc.sample is not None and exc.sample.attempts:
                    samples.append(exc.sample)  # 未完成，按失败计，费用计入合计
                print(f"{name}: 中止：{aborted}", file=out)
                break
            samples.append(s)
            trail = " → ".join(a.outcome for a in s.attempts)
            stats = s.stats or {}
            print(
                f"{name}: {'通过' if s.passed else '失败'}  修复 {s.repairs} 轮 [{trail}]  "
                f"镜头 {stats.get('shots', '-')} 时长 {stats.get('duration_s', '-')}s  ¥{s.cost_cny:.4f}  {s.elapsed_s:.0f}s",
                file=out,
            )
        balance_after = balance_fn(settings.provider)
        summary = summarize(samples, settings)
        summary.update(
            run_id=run.run_id,
            n_requested=n,
            spent_cny_total=round(gen.spent, 4),  # 与 calls.jsonl 的 cost_cny 合计一致
            logline=logline,
            logline_source=source,
            aborted=aborted,
            price=asdict(pricing.price_for(settings.provider, settings.model)),
            balance_before_cny=balance_before,
            balance_after_cny=balance_after,
            balance_delta_cny=(
                round(balance_before - balance_after, 2) if balance_before is not None and balance_after is not None else None
            ),
        )
        run.write_json("summary.json", summary)
        ok = aborted is None and summary["passed"] == n
        print(
            f"\n通过 {summary['passed']}/{n}（首次通过 {summary['first_pass']}），估算费用 ¥{summary['cost_cny_total']:.4f}"
            + (f"，余额差 ¥{summary['balance_delta_cny']:.2f}" if summary["balance_delta_cny"] is not None else ""),
            file=out,
        )
        print(f"输出：{run.dir}", file=out)
        if not ok:
            run.meta["result"] = "failed"
    return 0 if ok else 1


def _cmd(args: argparse.Namespace) -> int:
    settings = Settings(
        provider=args.provider,
        model=args.model,
        prompt_version=args.prompt_version,
        target_s=args.target_s,
        max_repairs=args.max_repairs,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        thinking=args.thinking,
        reasoning_effort=args.reasoning_effort,
        max_cost_cny=args.max_cost_cny,
    )
    return run_script(settings, n=args.n, logline=args.logline)


def add_parser(sub) -> None:
    p = sub.add_parser("script", help="一句话梗概 → 1 集 DramaIR v0 剧本（P0-03）")
    p.add_argument("--logline", help="一句话梗概；默认取标准样例 ep01 的 series.logline")
    p.add_argument("--n", type=int, default=5, help="顺序生成的样本数（默认 5）")
    p.add_argument("--provider", default="deepseek", help="OpenAI 兼容端点（见 poc/llm.py 的 ENDPOINTS）")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--prompt-version", default=DEFAULT_PROMPT, help="poc/prompts/<版本>.md")
    p.add_argument("--target-s", type=int, default=60, help="单集目标时长（秒）")
    p.add_argument("--max-repairs", type=int, default=2, help="每份最多自动修复几轮（默认 2）")
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--max-tokens", type=int, default=32000, help="含思维链 tokens")
    p.add_argument("--thinking", choices=("default", "enabled", "disabled"), default="default", help="DeepSeek 思维链开关")
    p.add_argument("--reasoning-effort", default=None, help="DeepSeek 思维强度，如 low / high / max")
    p.add_argument("--max-cost-cny", type=float, default=20.0, help="本次运行累计估算费用上限（元），达到即中止")
    p.set_defaults(func=_cmd)
