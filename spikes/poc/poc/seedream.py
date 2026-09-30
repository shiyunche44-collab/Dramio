"""火山方舟 Seedream 图像生成客户端（只用标准库，P0-06）。

接口：POST https://ark.cn-beijing.volces.com/api/v3/images/generations（文档 82379/1541523，2026-09-30 读取）
鉴权：`Authorization: Bearer <ARK_API_KEY>`。不跟随重定向（沿用 doctor 的 opener）。

请求要点（Seedream 5.0 pro / flash；4.0 另见各处注明）：
- `size`：档位 `1K` / `1.5K` / `2K`（默认 2K），或 `宽x高`（总像素 [921600, 4624220]，宽高比 [1/16, 16]；4.0 上限 4096x4096）；
- `image`：参考图，URL 或 `data:image/<小写格式>;base64,...`，最多 10 张；
- `response_format`：`url`（24 小时有效）或 `b64_json`；`output_format`：`jpeg`（默认）/ `png`（只有 5.0 支持，4.0 不传）；
- `watermark`：默认 true（右下角“AI 生成”），本项目定妆图取 false（D-005）；
- 5.0 pro / flash 不支持组图 `sequential_image_generation`、流式输出（4.0 支持，本项目不用）；文档没有 `seed` 参数。
响应：`data[]`（`b64_json` 或 `url`、`size`、`output_format`）、`usage`（`generated_images`、`input_images`、`output_tokens`）；
整个请求失败时返回 `error{code, message}`。

失败统一抛出 ImageError，kind 标明原因：
- network      DNS、连接、超时、代理拒绝、传输中途断开
- http         HTTP 4xx / 5xx（审核拒绝除外）
- moderation   审核不通过（错误码含 SensitiveContent / RiskDetection），不计费、不重试
- bad_response 响应不是预期结构
- empty        响应里没有图片
transient 属性标明是否可重试（网络、429、5xx、服务端繁忙）。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from poc import config, doctor, providers

BASE_URL = "https://ark.cn-beijing.volces.com"
PATH = "/api/v3/images/generations"
KEY_ENV = "ARK_API_KEY"
PROVIDER = "ark"
USER_AGENT = "dramio-poc-seedream/0"
DEFAULT_TIMEOUT = 180.0

MODELS = {
    "pro": "doubao-seedream-5-0-pro-260628",
    "flash": "doubao-seedream-5-0-flash-260915",
    "v4": "doubao-seedream-4-0-20260415",
}
# output_format 只有 5.0 系列支持（文档 82379/1541523）；4.0 不传，默认输出 jpeg
OUTPUT_FORMAT_MODELS = {MODELS["pro"], MODELS["flash"]}
MAX_REFS = 10
_MODERATION_MARKERS = ("SensitiveContent", "RiskDetection", "ContentFilter")
_TRANSIENT_CODES = ("ServerOverloaded", "InternalServiceError", "RateLimitExceeded", "QuotaExceeded.Concurrency")


class ImageError(Exception):
    def __init__(
        self, kind: str, message: str, http_status: int | None = None, api_code: str | None = None, transient: bool | None = None
    ):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.http_status = http_status
        self.api_code = api_code
        if transient is None:
            transient = (
                kind == "network"
                or (kind == "http" and (http_status == 429 or (http_status or 0) >= 500))
                or any(c in (api_code or "") for c in _TRANSIENT_CODES)
            )
        self.transient = transient

    @property
    def billable(self) -> bool:
        """保守计费口径：网络中断、响应无法解析、429、5xx 可能已生成，按全额计；审核拒绝、其它 4xx、无图不计费。"""
        if self.kind in ("network", "bad_response"):
            return True
        return self.kind == "http" and (self.http_status == 429 or (self.http_status or 0) >= 500)


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # 键为小写
    body: bytes


Transport = Callable[[Request, float], Response]


def _default_transport(request: Request, timeout: float) -> Response:
    try:
        with doctor.urlopen(request, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except HTTPError as exc:
        try:
            body = exc.read(8192)
        except Exception:
            body = b""
        return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
    except (URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise ImageError("network", f"{type(exc).__name__}: {reason}") from None


def data_uri(image: bytes, fmt: str) -> str:
    fmt = fmt.lower()
    if fmt == "jpg":
        fmt = "jpeg"
    return f"data:image/{fmt};base64,{base64.b64encode(image).decode('ascii')}"


def build_body(
    model: str,
    prompt: str,
    *,
    size: str,
    refs: list[str] | None = None,
    watermark: bool = False,
    response_format: str = "b64_json",
    output_format: str = "jpeg",
) -> dict[str, Any]:
    """refs：参考图（data URI 或 URL）；一张时传字符串，多张时传数组。"""
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "response_format": response_format,
        "watermark": watermark,
    }
    if model in OUTPUT_FORMAT_MODELS:
        body["output_format"] = output_format
    if refs:
        if len(refs) > MAX_REFS:
            raise ValueError(f"参考图最多 {MAX_REFS} 张")
        body["image"] = refs[0] if len(refs) == 1 else list(refs)
    return body


def _error_from(obj: Any) -> tuple[str | None, str]:
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = obj["error"]
        return (str(err.get("code")) if err.get("code") is not None else None), str(err.get("message") or "")[:300]
    return None, ""


_ACCOUNT_RE = re.compile(r"(account\s+)\d+", re.IGNORECASE)


def _scrub(text: str, secrets) -> str:
    """脱敏：密钥值，以及错误信息里的方舟账号 ID（如 ModelNotOpen 的“Your account 123…”）。"""
    return _ACCOUNT_RE.sub(r"\1***", config.redact(text, secrets))


def _classify(status: int, code: str | None, message: str, secrets) -> ImageError:
    text = _scrub(f"HTTP {status} code={code} {message}".strip(), secrets)
    if code and any(m in code for m in _MODERATION_MARKERS):
        return ImageError("moderation", text, http_status=status, api_code=code, transient=False)
    return ImageError("http", text, http_status=status, api_code=code)


@dataclass
class GeneratedImage:
    data: bytes | None  # b64_json 时为解码后的字节；url 时为 None（调用方自行下载）
    url: str | None
    size: str | None
    output_format: str | None


@dataclass
class ImageResult:
    images: list[GeneratedImage]
    usage: dict[str, Any]
    request_id: str | None
    model: str | None
    extra: dict[str, Any] = field(default_factory=dict)


def parse_response(body: bytes) -> tuple[list[GeneratedImage], dict[str, Any], str | None]:
    try:
        obj = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ImageError("bad_response", "响应不是 JSON") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
        code, message = _error_from(obj)
        if code:
            raise ImageError("http", f"code={code} {message}", http_status=200, api_code=code, transient=False)
        raise ImageError("bad_response", "响应缺少 data")
    images = []
    for i, item in enumerate(obj["data"]):
        if not isinstance(item, dict):
            raise ImageError("bad_response", f"data[{i}] 不是对象")
        if isinstance(item.get("error"), dict):
            continue  # 组图中单张失败（5.0 pro / flash 不会出现）
        raw = None
        if item.get("b64_json"):
            try:
                raw = base64.b64decode(item["b64_json"], validate=True)
            except ValueError:
                raise ImageError("bad_response", f"data[{i}].b64_json 不是 base64") from None
        elif not item.get("url"):
            raise ImageError("bad_response", f"data[{i}] 既没有 b64_json 也没有 url")
        images.append(GeneratedImage(raw, item.get("url"), item.get("size"), item.get("output_format")))
    if not images:
        code, message = _error_from(obj)
        raise ImageError("empty", f"没有图片 {code or ''} {message}".strip(), api_code=code, transient=False)
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    return images, usage, obj.get("model")


def generate(
    prompt: str,
    *,
    model: str,
    size: str,
    refs: list[str] | None = None,
    watermark: bool = False,
    response_format: str = "b64_json",
    output_format: str = "jpeg",
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport | None = None,
) -> ImageResult:
    env = os.environ if env is None else env
    key = (env.get(KEY_ENV) or "").strip()
    if not key:
        raise ImageError("config", f"缺少 {KEY_ENV}", transient=False)
    secrets = providers.secret_values(env)
    body = build_body(
        model, prompt, size=size, refs=refs, watermark=watermark, response_format=response_format, output_format=output_format
    )
    request = Request(
        BASE_URL + PATH,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        resp = (transport or _default_transport)(request, timeout)
    except ImageError as exc:
        exc.args = (_scrub(str(exc), secrets),)
        raise
    request_id = resp.headers.get("x-request-id") or resp.headers.get("x-tt-logid")
    if resp.status != 200:
        try:
            obj = json.loads(resp.body.decode("utf-8", "replace"))
        except ValueError:
            obj = None
        code, message = _error_from(obj)
        if not code and not message:
            message = resp.body[:300].decode("utf-8", "replace")
        raise _classify(resp.status, code, message, secrets)
    try:
        images, usage, model_echo = parse_response(resp.body)
    except ImageError as exc:
        if exc.api_code and any(m in exc.api_code for m in _MODERATION_MARKERS):
            raise ImageError(
                "moderation", _scrub(str(exc), secrets), http_status=200, api_code=exc.api_code, transient=False
            ) from None
        exc.args = (_scrub(str(exc), secrets),)
        raise
    return ImageResult(images, usage, request_id, model_echo)
