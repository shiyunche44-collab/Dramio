"""doctor：逐个检查供应商密钥是否可用。

状态：
- MISSING      未配置：缺少必需的环境变量
- CONFIGURED   已配置、未在线验证（--offline，或该供应商没有已确认的免费校验接口）
- OK           可用：探测接口返回 2xx（429 视为密钥有效但被限流）
- INVALID      密钥无效：返回 401 / 403
- UNREACHABLE  不可达：DNS、连接、超时、代理拒绝、TLS 错误（通常是网络策略未放行该域名）
- ERROR        其他：5xx 等

缺少密钥或网络被拦截都不算 doctor 失败；只有 --require 列出的供应商不是 OK 时才返回 1。
在线探测只用免费的只读 GET 接口，每次探测写入 calls.jsonl（cost_cny=0，cost_basis=free）。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import TextIO
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from poc import config, providers
from poc.providers import CAPABILITIES, Provider
from poc.runlog import Run

MISSING = "MISSING"
CONFIGURED = "CONFIGURED"
OK = "OK"
INVALID = "INVALID"
UNREACHABLE = "UNREACHABLE"
ERROR = "ERROR"

STATUS_LABELS = {
    MISSING: "未配置",
    CONFIGURED: "已配置",
    OK: "可用",
    INVALID: "无效",
    UNREACHABLE: "不可达",
    ERROR: "错误",
}

DEFAULT_TIMEOUT = 10.0
USER_AGENT = "dramio-poc-doctor/0"


@dataclass
class Result:
    provider: str
    label: str
    capabilities: list[str]
    status: str
    detail: str
    missing: list[str] = field(default_factory=list)
    http_status: int | None = None
    probe: str | None = None
    domains: list[str] = field(default_factory=list)


def _result(p: Provider, status: str, detail: str, **kw) -> Result:
    return Result(
        provider=p.name,
        label=p.label,
        capabilities=list(p.capabilities),
        status=status,
        detail=detail,
        probe=p.probe.description if p.probe else None,
        domains=list(p.domains),
        **kw,
    )


def classify_http(code: int, body: str = "") -> tuple[str, str]:
    if 200 <= code < 300:
        return OK, f"HTTP {code}"
    if code == 429:
        return OK, "HTTP 429：密钥有效，但被限流"
    if code in (401, 403):
        return INVALID, f"HTTP {code}：密钥无效或无权限"
    if code == 400 and "API_KEY_INVALID" in body:  # Google 对无效密钥返回 400
        return INVALID, "HTTP 400：API_KEY_INVALID，密钥无效"
    if 300 <= code < 400:
        return ERROR, f"HTTP {code}：重定向（为避免鉴权头外泄，不跟随）"
    return ERROR, f"HTTP {code}"


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def check_provider(
    p: Provider, env: Mapping[str, str], run: Run | None, offline: bool = False, timeout: float = DEFAULT_TIMEOUT
) -> Result:
    # 首尾空白（例如粘贴时多出的换行）不算密钥的一部分；只含空白视为未配置
    values = {v: (env.get(v) or "").strip() for v in (*p.env, *p.optional_env)}
    missing = [v for v in p.env if not values[v]]
    if missing:
        return _result(p, MISSING, "缺少 " + "、".join(missing), missing=missing)
    bad = [v for v in p.env if _has_control_chars(values[v])]
    if bad:
        return _result(p, INVALID, "、".join(bad) + " 含换行等非法字符，请检查（未发请求）")
    if offline:
        return _result(p, CONFIGURED, "离线模式，未在线验证")
    if p.probe is None:
        return _result(p, CONFIGURED, "无已确认的免费校验接口，只检查是否配置")

    secrets = providers.secret_values(env)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **p.probe.headers(values)}
    request = Request(p.probe.url, headers=headers, method="GET")
    code: int | None = None
    if run is None:
        status, detail, code = _probe(request, timeout)
        detail = config.redact(detail, secrets)
    else:
        with run.call(provider=p.name, capability="doctor.probe") as call:
            call.cost_cny = 0.0
            call.cost_basis = "free"
            call.extra = {"probe": p.probe.description}
            status, detail, code = _probe(request, timeout)
            detail = config.redact(detail, secrets)
            if status != OK:
                call.status = "error"
                call.error = f"{status}: {detail}"
            call.extra["http_status"] = code
    return _result(p, status, detail, http_status=code)


class _NoRedirect(HTTPRedirectHandler):
    """不跟随重定向：urllib 跟随时会把鉴权头带到新的主机。3xx 以 HTTPError 返回。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


def urlopen(request: Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _probe(request: Request, timeout: float) -> tuple[str, str, int | None]:
    """探测一次；任何异常都转换为状态，不向外抛出。"""
    try:
        with urlopen(request, timeout=timeout) as resp:
            code = resp.status
        body = ""
    except HTTPError as exc:  # 供应商返回了 HTTP 响应
        code = exc.code
        try:
            body = exc.read(4096).decode("utf-8", "replace")
        except Exception:
            body = ""
    except (URLError, OSError) as exc:  # 网络层失败：DNS、连接、超时、代理 CONNECT 被拒、TLS
        reason = getattr(exc, "reason", exc)
        return UNREACHABLE, f"网络不可达：{reason}（检查网络策略是否放行该域名）", None
    except Exception as exc:  # 兜底：只记录异常类型，异常消息可能含请求头（密钥）
        return ERROR, f"探测异常：{type(exc).__name__}", None
    status, detail = classify_http(code, body)
    return status, detail, code


def _safe_check(p: Provider, env: Mapping[str, str], run: Run, offline: bool, timeout: float) -> Result:
    """单个供应商出错不能拖垮整份报告；只记录异常类型，异常消息可能含密钥。"""
    try:
        return check_provider(p, env, run, offline=offline, timeout=timeout)
    except Exception as exc:
        return _result(p, ERROR, f"检查异常：{type(exc).__name__}")


def coverage(results: list[Result]) -> dict[str, dict[str, int]]:
    cov: dict[str, dict[str, int]] = {}
    for cap in CAPABILITIES:
        rows = [r for r in results if cap in r.capabilities]
        cov[cap] = {
            "total": len(rows),
            "ok": sum(r.status == OK for r in rows),
            "configured": sum(r.status in (OK, CONFIGURED) for r in rows),
        }
    return cov


def run_doctor(
    offline: bool = False,
    require: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    base_dir=None,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    require = require or []
    unknown = [n for n in require if n not in providers.BY_NAME]
    if unknown:
        print(f"未知的供应商：{', '.join(unknown)}；可选：{', '.join(providers.BY_NAME)}", file=out)
        return 2

    args = {"offline": offline, "require": require, "timeout": timeout}
    with Run("doctor", args, base_dir=base_dir, secrets=providers.secret_values(env)) as run:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda p: _safe_check(p, env, run, offline, timeout), providers.PROVIDERS))
        cov = coverage(results)
        failed = [n for n in require if next(r for r in results if r.provider == n).status != OK]
        run.write_json(
            "doctor.json",
            {
                "run_id": run.run_id,
                "offline": offline,
                "results": [asdict(r) for r in results],
                "coverage": cov,
                "require": require,
                "require_failed": failed,
            },
        )
        _print_report(results, cov, run, failed, require, out)
    return 1 if failed else 0


def _print_report(results, cov, run: Run, failed, require, out: TextIO) -> None:
    print(f"run_id: {run.run_id}", file=out)
    print(f"{'供应商':<12}{'状态':<20}{'能力':<32}说明", file=out)
    for r in results:
        caps = ",".join(r.capabilities)
        status = f"{r.status}({STATUS_LABELS[r.status]})"
        print(f"{r.provider:<12}{status:<20}{caps:<32}{r.detail}", file=out)
    print("", file=out)
    print("能力覆盖（可用 / 已配置 / 候选）：", file=out)
    for cap, c in cov.items():
        print(f"  {CAPABILITIES[cap]}：{c['ok']} / {c['configured']} / {c['total']}", file=out)
    if require:
        print("", file=out)
        print("--require：" + ("不满足：" + ", ".join(failed) if failed else "全部可用"), file=out)
    print("", file=out)
    print(f"输出：{run.dir}", file=out)


def _cmd(args: argparse.Namespace) -> int:
    require = [s.strip() for s in (args.require or "").split(",") if s.strip()]
    return run_doctor(offline=args.offline, require=require, timeout=args.timeout)


def add_parser(sub) -> None:
    parser = sub.add_parser("doctor", help="检查各供应商密钥是否可用（只用免费接口）")
    parser.add_argument("--offline", action="store_true", help="不访问网络，只检查变量是否已配置")
    parser.add_argument("--require", metavar="a,b", help="列出的供应商有任何一家不是 OK 时返回 1")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="每家探测的超时秒数")
    parser.set_defaults(func=_cmd)
