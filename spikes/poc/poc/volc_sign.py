"""火山引擎 OpenAPI V4 签名与豆包音乐（GenBGMForTime）客户端（只用标准库，P0-10）。

签名（doc 6369/67270）：HMAC-SHA256；派生密钥 HMAC(HMAC(HMAC(HMAC(SK, 短日期), region), service), "request")，
**起点就是 SK，不加 AWS 的 "AWS4" 前缀**；凭证范围 `<短日期>/<region>/<service>/request`。

音乐（doc 84992/2100970、2100960，全部是文档记载，P0-10 离线阶段没有真实调用过）：
    POST https://open.volcengineapi.com/?Action=GenBGMForTime&Version=2024-08-12   提交（异步，返回 Result.TaskID）
    POST https://open.volcengineapi.com/?Action=QuerySong&Version=2024-08-12       查询，Status 0 等待 / 1 处理中 / 2 成功 / 3 失败
    GET  https://open.volcengineapi.com/?Action=QueryUsage&Version=2024-08-12      用量（只读，可作免费校验）
    region cn-beijing，service imagination。

失败统一抛出 VolcError，kind 标明原因：
- config       缺少 AK / SK
- network      DNS、连接、超时、代理拒绝、传输中途断开
- http         HTTP 4xx / 5xx
- api_error    HTTP 200 但业务码非 0，或 ResponseMetadata.Error 非空
- bad_response 响应不是预期结构
- task_failed  任务 Status=3
- timeout      轮询超时
transient 属性标明是否可重试（网络、429、5xx、限流 / 队列满 / 语义识别错误）。
AK / SK 不会出现在任何异常信息里（config.redact 兜底）。
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

from poc import config, doctor

HOST = "open.volcengineapi.com"
BASE_URL = f"https://{HOST}/"
REGION = "cn-beijing"
SERVICE = "imagination"
API_VERSION = "2024-08-12"
ACTION_SUBMIT = "GenBGMForTime"  # 后付费；预付费资源包为 GenBGM
ACTION_QUERY = "QuerySong"
ACTION_USAGE = "QueryUsage"
AK_ENV = "VOLC_ACCESSKEY"
SK_ENV = "VOLC_SECRETKEY"
PROVIDER = "volc_music"
USER_AGENT = "dramio-poc-music/0"
DEFAULT_TIMEOUT = 60.0
ALGORITHM = "HMAC-SHA256"

STATUS_WAITING, STATUS_RUNNING, STATUS_OK, STATUS_FAILED = 0, 1, 2, 3

# 文档错误码表 doc 84992/1404675：限流、队列满、语义识别错误（文档写明需要重试）、服务内部错误
_TRANSIENT_API_CODES = {100001, 200023, 300067, 400040}


class VolcError(Exception):
    def __init__(
        self,
        kind: str,
        message: str,
        http_status: int | None = None,
        api_code: int | str | None = None,
        transient: bool | None = None,
    ):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.http_status = http_status
        self.api_code = api_code
        if transient is None:
            transient = kind == "network" or (kind == "http" and (http_status == 429 or (http_status or 0) >= 500)) or api_code in _TRANSIENT_API_CODES
        self.transient = transient


# ---- V4 签名 ----


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def format_x_date(moment: datetime) -> str:
    """`YYYYMMDD'T'HHMMSS'Z'`，UTC。"""
    if moment.tzinfo is None:
        raise ValueError("需要带时区的时间")
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def canonical_query(params: Mapping[str, str]) -> str:
    """按键排序，键和值按 RFC 3986 编码（空格为 %20，只保留 -_.~ 不编码）。"""
    return "&".join(f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(params.items()))


def canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """(CanonicalHeaders, SignedHeaders)：头名小写、值去首尾空白，按头名排序。"""
    items = sorted((k.strip().lower(), " ".join(str(v).split())) for k, v in headers.items())
    names = [k for k, _ in items]
    if len(set(names)) != len(names):
        raise ValueError("请求头名称（忽略大小写）重复")
    return "".join(f"{k}:{v}\n" for k, v in items), ";".join(names)


def canonical_request(method: str, path: str, query: Mapping[str, str], headers: Mapping[str, str], body: bytes) -> tuple[str, str]:
    """(规范请求, SignedHeaders)。"""
    ch, signed = canonical_headers(headers)
    text = "\n".join([method.upper(), path, canonical_query(query), ch, signed, sha256_hex(body)])
    return text, signed


def credential_scope(x_date: str, region: str = REGION, service: str = SERVICE) -> str:
    return f"{x_date[:8]}/{region}/{service}/request"


def string_to_sign(x_date: str, scope: str, canonical: str) -> str:
    return "\n".join([ALGORITHM, x_date, scope, sha256_hex(canonical.encode("utf-8"))])


def signing_key(secret: str, short_date: str, region: str = REGION, service: str = SERVICE) -> bytes:
    key = secret.encode("utf-8")
    for part in (short_date, region, service, "request"):
        key = hmac_sha256(key, part)
    return key


def sign(
    *,
    method: str,
    path: str = "/",
    query: Mapping[str, str],
    headers: Mapping[str, str],
    body: bytes,
    access_key: str,
    secret_key: str,
    x_date: str,
    region: str = REGION,
    service: str = SERVICE,
) -> str:
    """返回 Authorization 头的值。`headers` 里的全部头都参与签名（调用方负责只传要签的头，不含 Authorization）。"""
    if any(k.strip().lower() == "authorization" for k in headers):
        raise ValueError("Authorization 不能参与签名")
    canonical, signed = canonical_request(method, path, query, headers, body)
    scope = credential_scope(x_date, region, service)
    signature = hmac.new(signing_key(secret_key, x_date[:8], region, service), string_to_sign(x_date, scope, canonical).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"


# ---- 传输 ----


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # 键为小写
    body: bytes


# transport(request, timeout) -> Response；HTTP 错误也返回 Response，网络错误抛 VolcError。测试时注入假实现
Transport = Callable[[Request, float], Response]
Clock = Callable[[], datetime]


def _default_transport(request: Request, timeout: float) -> Response:
    try:
        with doctor.urlopen(request, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except HTTPError as exc:
        try:
            body = exc.read(4096)
        except Exception:
            body = b""
        return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
    except (URLError, OSError, http.client.HTTPException) as exc:  # 含超时、connection reset、IncompleteRead
        reason = getattr(exc, "reason", exc)
        raise VolcError("network", f"{type(exc).__name__}: {reason}") from None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str

    @property
    def secrets(self) -> list[str]:
        return [self.access_key, self.secret_key]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Credentials":
        ak, sk = (env.get(AK_ENV) or "").strip(), (env.get(SK_ENV) or "").strip()
        missing = [name for name, v in ((AK_ENV, ak), (SK_ENV, sk)) if not v]
        if missing:
            raise VolcError("config", f"缺少 {'、'.join(missing)}", transient=False)
        return cls(ak, sk)


def _call(
    action: str,
    body: Mapping[str, Any] | None,
    *,
    method: str,
    creds: Credentials,
    timeout: float,
    transport: Transport | None,
    clock: Clock | None,
) -> tuple[dict[str, Any], Response]:
    payload = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    query = {"Action": action, "Version": API_VERSION}
    x_date = format_x_date((clock or _utc_now)())
    # 签 host / x-date / x-content-sha256 / content-type（与官方 SDK 一致）
    signed_headers = {"Host": HOST, "X-Date": x_date, "X-Content-Sha256": sha256_hex(payload), "Content-Type": "application/json"}
    auth = sign(method=method, query=query, headers=signed_headers, body=payload, access_key=creds.access_key, secret_key=creds.secret_key, x_date=x_date)
    headers = {**signed_headers, "Authorization": auth, "User-Agent": USER_AGENT}
    request = Request(f"{BASE_URL}?{canonical_query(query)}", data=payload if method == "POST" else None, headers=headers, method=method)
    try:
        resp = (transport or _default_transport)(request, timeout)
    except VolcError as exc:
        exc.args = (config.redact(str(exc), creds.secrets),)
        raise
    return _parse(resp, creds), resp


def _parse(resp: Response, creds: Credentials) -> dict[str, Any]:
    text = resp.body.decode("utf-8", "replace")
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if resp.status != 200:
        detail = _error_detail(obj) if isinstance(obj, dict) else text[:300]
        code = _error_code(obj) if isinstance(obj, dict) else None
        raise VolcError("http", config.redact(f"HTTP {resp.status}：{detail}", creds.secrets), http_status=resp.status, api_code=code)
    if not isinstance(obj, dict):
        raise VolcError("bad_response", "响应不是 JSON 对象")
    meta = obj.get("ResponseMetadata") if isinstance(obj.get("ResponseMetadata"), dict) else {}
    err = meta.get("Error")
    code = obj.get("Code")
    if err or (code not in (None, 0)):
        raise VolcError("api_error", config.redact(_error_detail(obj), creds.secrets), api_code=_error_code(obj))
    return obj


def _error_code(obj: dict[str, Any]) -> int | str | None:
    meta = obj.get("ResponseMetadata") if isinstance(obj.get("ResponseMetadata"), dict) else {}
    err = meta.get("Error") if isinstance(meta.get("Error"), dict) else {}
    for value in (obj.get("Code"), err.get("CodeN"), err.get("Code")):
        if value not in (None, 0, ""):
            return value
    return None


def _error_detail(obj: dict[str, Any]) -> str:
    meta = obj.get("ResponseMetadata") if isinstance(obj.get("ResponseMetadata"), dict) else {}
    err = meta.get("Error") if isinstance(meta.get("Error"), dict) else {}
    parts = [str(p) for p in (obj.get("Code") if obj.get("Code") not in (None, 0) else None, err.get("Code"), obj.get("Message") or err.get("Message")) if p]
    return " ".join(parts)[:300] or "（无错误信息）"


# ---- 音乐接口 ----


@dataclass
class SubmitResult:
    task_id: str
    predicted_wait_s: float | None


@dataclass
class SongStatus:
    task_id: str
    status: int
    progress: int | None = None
    audio_url: str | None = None
    duration_s: float | None = None
    failure_code: int | None = None
    failure_msg: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status in (STATUS_OK, STATUS_FAILED)


def build_submit_body(
    text: str,
    duration_s: int,
    *,
    segments: list[tuple[str, int]] | None = None,
    aigc_watermark: bool | None = None,
    implicit_watermark: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """GenBGMForTime 请求体（v5.0）。只写文档明确的字段；未指定的可选项不发（`EnableInputRewrite`、`TosBucket`、`CallbackURL` 缺省即默认）。"""
    if not text.strip():
        raise ValueError("Text 不能为空")
    body: dict[str, Any] = {"Text": text, "Version": "v5.0"}
    if segments:
        if not all(5 <= d <= 120 for _, d in segments) or not 30 <= sum(d for _, d in segments) <= 120:
            raise ValueError("Segments：每段 [5,120] 秒、总和 [30,120] 秒")
        body["Segments"] = [{"Name": name, "Duration": d} for name, d in segments]
    else:
        if not 30 <= duration_s <= 120:
            raise ValueError("Duration：v5.0 单个时长有效范围 [30,120] 秒")
        body["Duration"] = int(duration_s)
    if aigc_watermark is not None:
        body["AigcWatermark"] = bool(aigc_watermark)
    if implicit_watermark:
        body["ImplicitWaterMark"] = dict(implicit_watermark)
    return body


def submit_bgm(
    body: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport | None = None,
    clock: Clock | None = None,
) -> SubmitResult:
    creds = Credentials.from_env(env)
    obj, _ = _call(ACTION_SUBMIT, body, method="POST", creds=creds, timeout=timeout, transport=transport, clock=clock)
    result = obj.get("Result")
    task_id = result.get("TaskID") if isinstance(result, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise VolcError("bad_response", "提交响应里没有 Result.TaskID")
    wait = result.get("PredictedWaitTime")
    return SubmitResult(task_id, float(wait) if isinstance(wait, (int, float)) else None)


def query_song(
    task_id: str,
    *,
    env: Mapping[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport | None = None,
    clock: Clock | None = None,
) -> SongStatus:
    creds = Credentials.from_env(env)
    obj, _ = _call(ACTION_QUERY, {"TaskID": task_id}, method="POST", creds=creds, timeout=timeout, transport=transport, clock=clock)
    result = obj.get("Result")
    if not isinstance(result, dict) or not isinstance(result.get("Status"), int):
        raise VolcError("bad_response", "查询响应里没有 Result.Status")
    detail = result.get("SongDetail") if isinstance(result.get("SongDetail"), dict) else {}
    fail = result.get("FailureReason") if isinstance(result.get("FailureReason"), dict) else {}
    duration = detail.get("Duration")
    progress = result.get("Progress")
    return SongStatus(
        task_id=str(result.get("TaskID") or task_id),
        status=result["Status"],
        progress=progress if isinstance(progress, int) else None,
        audio_url=detail.get("AudioUrl") or None,
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        failure_code=fail.get("Code") if isinstance(fail.get("Code"), int) else None,
        failure_msg=str(fail.get("Msg") or ""),
        raw=_scrub(result),
    )


def _scrub(result: dict[str, Any]) -> dict[str, Any]:
    """留证据用：去掉 AudioUrl 的查询串（可能带临时签名）。"""
    out = json.loads(json.dumps(result, ensure_ascii=False))
    detail = out.get("SongDetail")
    if isinstance(detail, dict) and isinstance(detail.get("AudioUrl"), str):
        detail["AudioUrl"] = detail["AudioUrl"].split("?", 1)[0]
    return out


def poll_song(
    task_id: str,
    *,
    env: Mapping[str, str],
    interval_s: float = 3.0,
    timeout_s: float = 300.0,
    transport: Transport | None = None,
    clock: Clock | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SongStatus:
    """轮询到 Status 2 或 3；3 抛 task_failed（失败码按文档码表判断是否可重试）。"""
    deadline = monotonic() + timeout_s
    while True:
        st = query_song(task_id, env=env, transport=transport, clock=clock)
        if st.status == STATUS_OK:
            return st
        if st.status == STATUS_FAILED:
            raise VolcError("task_failed", f"任务 {task_id} 失败：{st.failure_code} {st.failure_msg}", api_code=st.failure_code)
        if monotonic() >= deadline:
            raise VolcError("timeout", f"任务 {task_id} 超过 {timeout_s:g} 秒仍未完成（Status={st.status}，Progress={st.progress}）", transient=False)
        sleep(interval_s)


def query_usage(
    *,
    env: Mapping[str, str],
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """用量与授权状态（只读）。文档没写计费，推断免费；可作 AK/SK 与开通状态的校验。"""
    creds = Credentials.from_env(env)
    obj, _ = _call(ACTION_USAGE, None, method="GET", creds=creds, timeout=timeout, transport=transport, clock=clock)
    result = obj.get("Result")
    if not isinstance(result, dict):
        raise VolcError("bad_response", "用量响应里没有 Result")
    return result


def download(url: str, *, timeout: float = DEFAULT_TIMEOUT, transport: Transport | None = None, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    """GET 音频产物（不带鉴权头）。文档说默认 wav，但链路可能转码成 mp4 等，调用方落盘前要 ffprobe。"""
    if not url.startswith("https://"):
        raise VolcError("bad_response", "产物地址不是 https")
    resp = (transport or _default_transport)(Request(url, headers={"User-Agent": USER_AGENT}, method="GET"), timeout)
    if resp.status != 200:
        raise VolcError("http", f"下载失败 HTTP {resp.status}", http_status=resp.status)
    if not resp.body:
        raise VolcError("bad_response", "下载内容为空")
    if len(resp.body) > max_bytes:
        raise VolcError("bad_response", f"下载内容超过 {max_bytes} 字节")
    return resp.body
