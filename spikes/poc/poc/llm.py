"""最小的 OpenAI 兼容 Chat Completions 客户端（只用标准库）。

P0 验证用：只登记本步可用的 DeepSeek，其他 OpenAI 兼容端点（DashScope、Ark 等）拿到密钥后在 ENDPOINTS 中补一行即可。
不跟随重定向（沿用 doctor 的 opener），以免鉴权头被带到其他主机。

以下情况都抛出 LLMError（kind 标明原因），由调用方计为一次失败的尝试：
- http        供应商返回 4xx / 5xx
- network     DNS、连接、超时、代理拒绝
- truncated   finish_reason=length，输出被截断
- empty       content 为空
- bad_response 响应不是预期的 JSON 结构
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from poc import config, doctor, providers

USER_AGENT = "dramio-poc-llm/0"
DEFAULT_TIMEOUT = 600.0


@dataclass(frozen=True)
class Endpoint:
    provider: str  # providers.BY_NAME 中的名称
    url: str
    key_env: str


ENDPOINTS: dict[str, Endpoint] = {
    "deepseek": Endpoint("deepseek", "https://api.deepseek.com/chat/completions", "DEEPSEEK_API_KEY"),
}


class LLMError(Exception):
    def __init__(self, kind: str, message: str, http_status: int | None = None, usage: dict | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.http_status = http_status
        self.usage = usage  # 截断、空输出时供应商已计费，仍需入账


@dataclass
class ChatResult:
    text: str
    finish_reason: str | None
    usage: dict[str, Any]
    request_id: str | None
    model: str | None
    reasoning_chars: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# transport(request, timeout) -> (http_status, body_bytes)；测试时注入假实现
Transport = Callable[[Request, float], tuple[int, bytes]]


def _default_transport(request: Request, timeout: float) -> tuple[int, bytes]:
    with doctor.urlopen(request, timeout=timeout) as resp:
        return resp.status, resp.read()


def chat(
    provider: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    json_mode: bool = True,
    max_tokens: int | None = None,
    temperature: float | None = None,
    extra_body: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport | None = None,
) -> ChatResult:
    env = os.environ if env is None else env
    endpoint = ENDPOINTS[provider]
    key = (env.get(endpoint.key_env) or "").strip()
    if not key:
        raise LLMError("config", f"缺少 {endpoint.key_env}")
    secrets = providers.secret_values(env)

    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    body.update(extra_body or {})
    request = Request(
        endpoint.url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        status, raw = (transport or _default_transport)(request, timeout)
    except HTTPError as exc:
        try:
            detail = exc.read(2048).decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise LLMError("http", config.redact(f"HTTP {exc.code} {detail}".strip(), secrets), http_status=exc.code) from None
    except (URLError, OSError) as exc:  # 包括超时
        reason = getattr(exc, "reason", exc)
        raise LLMError("network", config.redact(f"{type(exc).__name__}: {reason}", secrets)) from None
    if not 200 <= status < 300:
        raise LLMError("http", f"HTTP {status}", http_status=status)

    try:
        data = json.loads(raw)
        choice = data["choices"][0]
        message = choice["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("bad_response", f"无法解析响应（{len(raw)} 字节）") from None

    usage = data.get("usage") or {}
    finish = choice.get("finish_reason")
    text = message.get("content") or ""
    if finish == "length":
        raise LLMError("truncated", f"输出被截断（completion_tokens={usage.get('completion_tokens')}）", usage=usage)
    if not text.strip():
        raise LLMError("empty", f"content 为空（finish_reason={finish}）", usage=usage)
    return ChatResult(
        text=text,
        finish_reason=finish,
        usage=usage,
        request_id=data.get("id"),
        model=data.get("model"),
        reasoning_chars=len(message.get("reasoning_content") or ""),
        raw=data,
    )


def balance_cny(provider: str, env: Mapping[str, str] | None = None, timeout: float = 15.0) -> float | None:
    """账户余额（元），用于按余额差复核费用；免费只读接口。取不到时返回 None，不抛异常。"""
    if provider != "deepseek":
        return None
    env = os.environ if env is None else env
    key = (env.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        return None
    request = Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with doctor.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return sum(float(b["total_balance"]) for b in data["balance_infos"] if b.get("currency") == "CNY")
    except Exception:
        return None
