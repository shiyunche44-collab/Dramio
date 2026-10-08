"""MiniMax 海螺视频客户端：H3 / H3-Max（v2）与 Hailuo-2.3（v1），提交、查询、下载（只用标准库，P0-08）。

也是 poc/video.py 的公共层：VideoError、Response / Transport、TaskState、Submitted、scrub、下载与响应解析的工具函数，
seedance.py 直接复用（与 costume → keyframe 的做法一致）。

站点与鉴权：国内站 `https://api.minimaxi.com`，`Authorization: Bearer <MINIMAX_API_KEY>`；国际站 api.minimax.io 用不了本 Key
（2026-09-30 实测 invalid api key）。文档页（platform.minimax.cn/docs，2026-09-30 读取）的 OpenAPI servers 写的是 api.minimax.cn，
与实测可用的 api.minimaxi.com 不同，以实测为准。

接口（文档副本要点）：
- H3 / H3-Max：`POST /v2/video_generation`，体 {model, content[文本 + 首帧图], resolution, duration, ratio, aigc_watermark, extra}；
  返回 {"task_id"}；查询 `GET /v2/query/video_generation/{task_id}` → {"task": {id, model, status, error{code,message},
  content{url}, resolution, duration, usage{total_seconds, output_seconds, input_image_count, …}, ratio, task_type}}。
  早先实测用 query 参数形式 `?task_id=` 返回 {"items": [...], "total": 1}，解析同时兼容两种形态。
  status：queued / running / succeeded / failed / cancelled。content.url 是限时下载链接，任务记录保留 7 天。
  H3：resolution 768P / 2K，duration 4–15；H3-Max：480P / 768P，duration 5–15，extra.prompt_expansion_mode（disabled / balanced / quality）。
  图生视频 ratio 恒为 adaptive（由首帧决定）。首帧图 `{"type":"image_url","image_url":{"url": data URI},"role":"first_frame"}`，
  限制：≤30 MB、宽高 256–5760、宽高比 0.4–2.5、请求体 ≤64 MB；text 必填且 ≤7000 字符。限流 300 RPM，最多 30 个并行任务。
- Hailuo-2.3（v1）：`POST /v1/video_generation`，体 {model, prompt, first_frame_image, duration(6|10), resolution, aigc_watermark}；
  查询 `GET /v1/query/video_generation?task_id=` → {status: Preparing/Queueing/Processing/Success/Fail, file_id, video_width,
  video_height, base_resp}；取回 `GET /v1/files/retrieve?file_id=` → {file{download_url（1 小时有效）}}。v1 的业务错误在 HTTP 200
  的 base_resp.status_code 里。首帧 <20 MB、短边 >300、宽高比 2:5–5:2，prompt ≤2000 字符；限流 20 RPM。
  v1 的 `[指令]` 运镜语法、H3 的方括号镜头指令都没有核实适用范围，提示词里不使用。
- 错误：HTTP 错误体 {"type":"error","error":{"type","message","http_code"},"request_id"}，message 末尾括号里是内部错误码。

失败统一抛 VideoError，kind 标明原因：
- network       DNS、连接、超时、代理拒绝、传输中途断开（可重试）
- rate_limit    429 / 业务码 1002（可重试）
- http          其它 HTTP 错误：400 参数错误不重试，5xx / 529 可重试
- account       账号级：401 / 鉴权失败、402 / 余额不足；换哪个镜头都会失败，调用方应中止整次运行
- moderation    422 / 业务码 1026、1027：输入或产物命中审核，不计费、不重试
- task_failed   任务状态 failed / cancelled / expired（非审核原因）
- timeout       轮询超过上限，任务仍在排队或运行；任务可能仍会成功，用 --resume 只查询
- download      下载视频失败（消息带主机名，不带 URL 查询串）；任务已成功，可重新查询后重试下载
- bad_response  响应不是预期结构
- bad_media     下载到的文件 ffprobe 无法解析（由 video.py 抛）
- config        本地校验不通过（model / resolution / duration 组合、首帧大小与尺寸、提示词为空或过长）或缺少 Key：不发请求

安全：任何会创建任务的请求都必须先经 VideoRequest 的本地校验（构造时校验，提交前再校验一次）；校验不通过不发请求。
下载链接可能带签名，所以下载请求不带 Authorization 头，任何记录（错误信息、日志、tasks.jsonl）里都只出现主机名、不出现 URL 查询串。
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
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from poc import config, doctor, images, providers

BASE_URL = "https://api.minimaxi.com"
KEY_ENV = "MINIMAX_API_KEY"
PROVIDER = "minimax"
USER_AGENT = "dramio-poc-video/0"
DEFAULT_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 300.0
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_EXTRA_CHARS = 300
MAX_EXTRA_KEYS = 20

MODELS = {"h3": "MiniMax-H3", "h3max": "MiniMax-H3-Max", "hailuo23": "MiniMax-Hailuo-2.3"}
PROMPT_EXPANSION_MODES = ("disabled", "balanced", "quality")

STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "expired")
TERMINAL = ("succeeded", "failed", "cancelled", "expired")
_STATUS_ALIASES = {
    "queued": "queued", "queueing": "queued", "queuing": "queued", "preparing": "queued", "pending": "queued",
    "running": "running", "processing": "running", "in_progress": "running",
    "succeeded": "succeeded", "success": "succeeded", "completed": "succeeded",
    "failed": "failed", "fail": "failed", "error": "failed",
    "cancelled": "cancelled", "canceled": "cancelled", "expired": "expired",
}

# 业务错误码（文档 base_resp.status_code / v2 错误体括号内码 / 任务 error.code）
ACCOUNT_CODES = ("1004", "1008", "2049")
MODERATION_CODES = ("1026", "1027")
RATE_LIMIT_CODES = ("1002",)
TRANSIENT_CODES = ("1000", "1001", "1013")
_TRAILING_CODE = re.compile(r"\((\d{3,5})\)\s*$")
_URL_QUERY = re.compile(r"(https?://[^\s\"'<>?#]+)[?#][^\s\"'<>]*")
_TASK_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


class VideoError(Exception):
    def __init__(
        self, kind: str, message: str, http_status: int | None = None, api_code: str | None = None, transient: bool | None = None
    ):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.http_status = http_status
        self.api_code = api_code
        if transient is None:
            transient = kind in ("network", "rate_limit") or (kind == "http" and (http_status or 0) >= 500)
        self.transient = transient

    @property
    def fatal(self) -> bool:
        """账号级或配置错误：调用方应中止整次运行。"""
        return self.kind in ("account", "config")

    @property
    def billable(self) -> bool:
        """提交失败时的保守计费口径：网络中断、响应无法解析、5xx 时任务可能已创建，按全额计；其余（审核、参数、鉴权、限流）不计费。"""
        return self.kind in ("network", "bad_response") or (self.kind == "http" and (self.http_status or 0) >= 500)


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # 键为小写
    body: bytes


Transport = Callable[[Request, float], Response]


def scrub(text: str, secrets: list[str] | None = None) -> str:
    """脱敏：密钥值，以及文本里 URL 的查询串 / 片段（下载链接可能带签名）。"""
    return _URL_QUERY.sub(r"\1", config.redact(text, secrets or []))


def host_of(url: str) -> str:
    return urlsplit(url).hostname or "?"


def _default_transport(request: Request, timeout: float) -> Response:
    try:
        with doctor.urlopen(request, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except HTTPError as exc:
        try:
            body = exc.read(16384)
        except Exception:
            body = b""
        return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
    except (URLError, OSError, http.client.HTTPException) as exc:
        raise VideoError("network", f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


def _download_transport(request: Request, timeout: float) -> Response:
    """下载用：跟随重定向（请求不带任何鉴权头，不会把密钥带到别的主机），限制体积。"""
    try:
        with build_opener().open(request, timeout=timeout) as resp:
            body = resp.read(MAX_DOWNLOAD_BYTES + 1)
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, body)
    except HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, b"")
    except (URLError, OSError, http.client.HTTPException) as exc:
        raise VideoError("network", f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


# ---- 图像与请求校验 ----


def data_uri(image: bytes, fmt: str) -> str:
    fmt = fmt.lower()
    if fmt == "jpg":
        fmt = "jpeg"
    return f"data:image/{fmt};base64,{base64.b64encode(image).decode('ascii')}"


@dataclass(frozen=True)
class ModelSpec:
    api: str  # v2 / v1
    durations: Mapping[str, tuple[int, ...]]  # 分辨率 → 允许的时长（秒）
    prompt_max: int
    image_max_bytes: int
    image_min_side: int
    image_max_side: int
    image_min_ratio: float = 0.4
    image_max_ratio: float = 2.5
    body_max_bytes: int = 64 * 1024 * 1024

    @property
    def resolutions(self) -> tuple[str, ...]:
        return tuple(self.durations)


_V2_IMAGE = dict(image_max_bytes=30 * 1024 * 1024, image_min_side=256, image_max_side=5760)
SPECS: dict[str, ModelSpec] = {
    "MiniMax-H3": ModelSpec("v2", {"768P": tuple(range(4, 16)), "2K": tuple(range(4, 16))}, 7000, **_V2_IMAGE),
    "MiniMax-H3-Max": ModelSpec("v2", {"480P": tuple(range(5, 16)), "768P": tuple(range(5, 16))}, 7000, **_V2_IMAGE),
    # v1：首帧 <20 MB（文档写“小于”，这里按 ≤20 MB − 1 字节）、短边 >300（即 ≥301）；边长上限文档未写，沿用 5760 作保守上界
    "MiniMax-Hailuo-2.3": ModelSpec(
        "v1", {"768P": (6, 10), "1080P": (6,)}, 2000,
        image_max_bytes=20 * 1024 * 1024 - 1, image_min_side=301, image_max_side=5760,
    ),
}


def spec_of(model: str) -> ModelSpec:
    try:
        return SPECS[model]
    except KeyError:
        raise VideoError("config", f"未知的 MiniMax 视频模型 {model!r}（可选 {', '.join(SPECS)}）", transient=False) from None


def allowed_durations(model: str, resolution: str) -> tuple[int, ...]:
    spec = spec_of(model)
    if resolution not in spec.durations:
        raise VideoError("config", f"{model} 不支持分辨率 {resolution!r}（可选 {' / '.join(spec.resolutions)}）", transient=False)
    return spec.durations[resolution]


def _config_error(message: str) -> VideoError:
    return VideoError("config", message, transient=False)


def validate_request(
    model: str, resolution: str, duration: int, prompt: str, image: bytes, prompt_expansion_mode: str | None = None
) -> images.ImageInfo:
    """本地校验（不发请求）；不合法抛 VideoError("config")，合法返回首帧的 ImageInfo。"""
    spec = spec_of(model)
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise _config_error(f"duration 必须是整数秒：{duration!r}")
    allowed = allowed_durations(model, resolution)
    if duration not in allowed:
        raise _config_error(f"{model} {resolution} 的 duration 只能取 {allowed[0]}–{allowed[-1]}（{'、'.join(map(str, allowed))}），收到 {duration}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _config_error("提示词（text）不能为空")
    if len(prompt) > spec.prompt_max:
        raise _config_error(f"提示词 {len(prompt)} 字符，超过 {spec.prompt_max}")
    if prompt_expansion_mode is not None:
        if model != "MiniMax-H3-Max":
            raise _config_error("extra.prompt_expansion_mode 只有 MiniMax-H3-Max 支持")
        if prompt_expansion_mode not in PROMPT_EXPANSION_MODES:
            raise _config_error(f"prompt_expansion_mode 必须是 {' / '.join(PROMPT_EXPANSION_MODES)}：{prompt_expansion_mode!r}")
    try:
        info = images.image_info(image)
    except images.ImageFormatError as exc:
        raise _config_error(f"首帧不是有效的 JPEG / PNG：{exc}") from None
    if info.bytes > spec.image_max_bytes:
        raise _config_error(f"首帧 {info.bytes} 字节，超过上限 {spec.image_max_bytes}")
    for name, side in (("宽", info.width), ("高", info.height)):
        if not spec.image_min_side <= side <= spec.image_max_side:
            raise _config_error(f"首帧{name} {side}px 不在 {spec.image_min_side}–{spec.image_max_side} 内")
    ratio = info.width / info.height
    if not spec.image_min_ratio <= ratio <= spec.image_max_ratio:
        raise _config_error(f"首帧宽高比 {ratio:.3f} 不在 {spec.image_min_ratio}–{spec.image_max_ratio} 内")
    if info.bytes * 4 // 3 + len(prompt.encode("utf-8")) + 4096 > spec.body_max_bytes:
        raise _config_error("请求体估算超过 64 MB")
    return info


@dataclass
class VideoRequest:
    """一个图生视频请求。构造时本地校验；Client.submit 发送前再校验一次，不合法的请求发不出去。"""

    model: str
    resolution: str
    duration: int
    prompt: str
    image: bytes = field(repr=False)
    prompt_expansion_mode: str | None = None  # 仅 H3-Max；None 表示不传 extra（服务端默认 balanced）
    aigc_watermark: bool = False  # 文档默认 false；P0 的中间产物不加水印，成片标识由 P0-11 负责（INV-08 只约束生产环境）

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> images.ImageInfo:
        return validate_request(self.model, self.resolution, self.duration, self.prompt, self.image, self.prompt_expansion_mode)

    @property
    def api(self) -> str:
        return spec_of(self.model).api

    @property
    def ratio(self) -> str:
        return "adaptive"  # 图生视频恒为 adaptive，由首帧决定

    @property
    def endpoint(self) -> tuple[str, str]:
        return ("POST", "/v2/video_generation" if self.api == "v2" else "/v1/video_generation")

    def body(self, image_uri: str | None = None) -> dict[str, Any]:
        """请求体；image_uri 默认为首帧的 data URI（dry-run 传占位文本，避免打印 base64）。"""
        info = images.image_info(self.image)
        uri = image_uri if image_uri is not None else data_uri(self.image, info.fmt)
        if self.api == "v1":
            return {
                "model": self.model, "prompt": self.prompt, "first_frame_image": uri, "duration": self.duration,
                "resolution": self.resolution, "aigc_watermark": self.aigc_watermark,
            }
        body: dict[str, Any] = {
            "model": self.model,
            "content": [
                {"type": "text", "text": self.prompt},
                {"type": "image_url", "image_url": {"url": uri}, "role": "first_frame"},
            ],
            "resolution": self.resolution,
            "duration": self.duration,
            "ratio": self.ratio,
            "aigc_watermark": self.aigc_watermark,
        }
        if self.prompt_expansion_mode is not None:
            body["extra"] = {"prompt_expansion_mode": self.prompt_expansion_mode}
        return body

    def preview(self) -> dict[str, Any]:
        """不含 base64 的请求预览：图像只显示大小 / sha256。"""
        info = images.image_info(self.image)
        return self.body(f"<data:image/{info.fmt};base64, {info.bytes} bytes, sha256={info.sha256}>")


# ---- 响应解析 ----


@dataclass
class Submitted:
    task_id: str
    request_id: str | None = None


@dataclass
class TaskState:
    task_id: str
    status: str  # STATUSES 之一；未知状态原样保留（按非终态处理）
    model: str | None = None
    video_url: str | None = field(default=None, repr=False)  # 限时下载链接，可能带签名：不写入任何记录
    file_id: str | None = None  # v1：需再调 files/retrieve 取下载地址
    error_code: str | None = None
    error_message: str | None = None
    resolution: str | None = None
    duration: int | None = None
    ratio: str | None = None
    width: int | None = None
    height: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    created_at: int | None = None
    updated_at: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # 未识别的字段：脱敏、限长后保存
    request_id: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def as_log(self) -> dict[str, Any]:
        """写入记录的形态：不含下载链接，只保留主机名。"""
        return {
            "task_id": self.task_id, "status": self.status, "model": self.model, "file_id": self.file_id,
            "video_host": host_of(self.video_url) if self.video_url else None,
            "error_code": self.error_code, "error_message": self.error_message, "resolution": self.resolution,
            "duration": self.duration, "ratio": self.ratio, "width": self.width, "height": self.height, "usage": self.usage,
            "created_at": self.created_at, "updated_at": self.updated_at, "extra": self.extra,
        }


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise VideoError("bad_response", "响应不是 JSON") from None


def _str(value: Any) -> str | None:
    return str(value) if value is not None and value != "" else None


def sanitize_extra(obj: Mapping[str, Any], known: set[str], secrets: list[str] | None = None) -> dict[str, Any]:
    """未识别字段：序列化成文本，去掉 URL 查询串与密钥，限长（MAX_EXTRA_CHARS），最多 MAX_EXTRA_KEYS 个。"""
    out: dict[str, Any] = {}
    for key in sorted(k for k in obj if k not in known)[:MAX_EXTRA_KEYS]:
        text = scrub(json.dumps(obj[key], ensure_ascii=False, default=str), secrets)
        if ";base64," in text:
            text = "<base64 数据已省略>"
        out[str(key)[:64]] = text[:MAX_EXTRA_CHARS]
    return out


def classify_code(code: str | None, message: str, secrets: list[str] | None = None, http_status: int | None = None) -> VideoError:
    """按业务错误码分类（v1 的 base_resp.status_code、v2 错误体括号内的码、任务的 error.code）。"""
    text = scrub(f"code={code} {message}".strip(), secrets)[:400]
    if code in ACCOUNT_CODES:
        return VideoError("account", text, http_status=http_status, api_code=code, transient=False)
    if code in MODERATION_CODES:
        return VideoError("moderation", text, http_status=http_status, api_code=code, transient=False)
    if code in RATE_LIMIT_CODES:
        return VideoError("rate_limit", text, http_status=http_status, api_code=code, transient=True)
    return VideoError("http", text, http_status=http_status, api_code=code, transient=code in TRANSIENT_CODES)


def classify_http(status: int, body: bytes, secrets: list[str] | None = None) -> VideoError:
    """HTTP 错误：体为 {"type":"error","error":{type,message,http_code},"request_id"}；体不是 JSON 时只按状态码。"""
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        obj = None
    err = obj.get("error") if isinstance(obj, dict) and isinstance(obj.get("error"), dict) else {}
    etype = str(err.get("type") or "")
    message = str(err.get("message") or "") or body[:200].decode("utf-8", "replace")
    m = _TRAILING_CODE.search(message)
    code = m.group(1) if m else None
    text = scrub(f"HTTP {status} type={etype or '-'} code={code or '-'} {message}".strip(), secrets)[:400]
    if status == 401 or etype == "authorized_error" or code in ("1004", "2049"):
        return VideoError("account", text, http_status=status, api_code=code, transient=False)
    if status == 402 or etype == "insufficient_balance_error" or code == "1008":
        return VideoError("account", text, http_status=status, api_code=code, transient=False)
    if status == 422 or etype == "unprocessable_entity_error" or code in MODERATION_CODES:
        return VideoError("moderation", text, http_status=status, api_code=code, transient=False)
    if status == 429 or etype == "rate_limit_error" or code in RATE_LIMIT_CODES:
        return VideoError("rate_limit", text, http_status=status, api_code=code, transient=True)
    if status >= 500 or etype in ("overloaded_error", "server_error"):
        return VideoError("http", text, http_status=status, api_code=code, transient=True)
    return VideoError("http", text, http_status=status, api_code=code, transient=False)  # 400 参数错误等：不重试


def _check_base_resp(obj: Mapping[str, Any], secrets: list[str] | None) -> None:
    """v1 的业务错误在 HTTP 200 的 base_resp 里；status_code 非 0 即失败。"""
    base = obj.get("base_resp")
    if isinstance(base, dict) and base.get("status_code") not in (None, 0, "0"):
        raise classify_code(_str(base.get("status_code")), str(base.get("status_msg") or ""), secrets, http_status=200)


def parse_submit(body: bytes, secrets: list[str] | None = None) -> str:
    obj = _json(body)
    if not isinstance(obj, dict):
        raise VideoError("bad_response", "响应不是对象")
    _check_base_resp(obj, secrets)
    task_id = _str(obj.get("task_id"))
    if task_id is None and isinstance(obj.get("task"), dict):
        task_id = _str(obj["task"].get("id"))
    if task_id is None:
        raise VideoError("bad_response", "响应缺少 task_id")
    if not _TASK_ID.match(task_id):
        raise VideoError("bad_response", "task_id 格式异常")
    return task_id


_V2_KNOWN = {
    "id", "model", "status", "error", "created_at", "updated_at", "content", "resolution", "duration", "usage", "ratio", "task_type", "modality",
}
_V1_KNOWN = {"task_id", "status", "file_id", "video_width", "video_height", "base_resp"}


def _normalize_status(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VideoError("bad_response", "响应缺少 status")
    return _STATUS_ALIASES.get(value.strip().lower(), value.strip().lower())


def _usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k)[:64]: v for k, v in list(value.items())[:MAX_EXTRA_KEYS] if isinstance(v, (int, float, str, bool)) or v is None}


def _pick_task(obj: Any, task_id: str) -> Mapping[str, Any]:
    """兼容三种形态：{"task": {...}}、{"items": [{...}], "total": 1}、任务字段平铺。"""
    if not isinstance(obj, dict):
        raise VideoError("bad_response", "响应不是对象")
    if isinstance(obj.get("task"), dict):
        return obj["task"]
    items = obj.get("items")
    if isinstance(items, list):
        dicts = [i for i in items if isinstance(i, dict)]
        match = [i for i in dicts if str(i.get("id")) == task_id]
        if match:
            return match[0]
        if len(dicts) == 1:
            return dicts[0]
        raise VideoError("bad_response", f"查询结果里没有任务 {task_id}（items {len(items)} 项）")
    if "status" in obj:
        return obj
    raise VideoError("bad_response", "响应缺少 task / items / status")


def _v2_extra(task: Mapping[str, Any], content: Mapping[str, Any], secrets: list[str] | None) -> dict[str, Any]:
    extra = sanitize_extra(task, _V2_KNOWN, secrets)
    for key, text in sanitize_extra(content, {"url", "video_url"}, secrets).items():  # content 里除下载链接外的未知字段
        extra[f"content.{key}"] = text
    return extra


def parse_task_v2(body: bytes, task_id: str, secrets: list[str] | None = None, request_id: str | None = None) -> TaskState:
    task = _pick_task(_json(body), task_id)
    error = task.get("error") if isinstance(task.get("error"), dict) else {}
    content = task.get("content") if isinstance(task.get("content"), dict) else {}
    url = _str(content.get("url")) or _str(content.get("video_url")) or _str(task.get("video_url"))
    return TaskState(
        task_id=_str(task.get("id")) or task_id,
        status=_normalize_status(task.get("status")),
        model=_str(task.get("model")),
        video_url=url,
        error_code=_str(error.get("code")),
        error_message=scrub(str(error.get("message") or ""), secrets)[:MAX_EXTRA_CHARS] or None,
        resolution=_str(task.get("resolution")),
        duration=task.get("duration") if isinstance(task.get("duration"), int) else None,
        ratio=_str(task.get("ratio")),
        usage=_usage(task.get("usage")),
        created_at=task.get("created_at") if isinstance(task.get("created_at"), int) else None,
        updated_at=task.get("updated_at") if isinstance(task.get("updated_at"), int) else None,
        extra=_v2_extra(task, content, secrets),
        request_id=request_id,
    )


def parse_task_v1(body: bytes, task_id: str, secrets: list[str] | None = None, request_id: str | None = None) -> TaskState:
    obj = _json(body)
    if not isinstance(obj, dict):
        raise VideoError("bad_response", "响应不是对象")
    _check_base_resp(obj, secrets)
    return TaskState(
        task_id=_str(obj.get("task_id")) or task_id,
        status=_normalize_status(obj.get("status")),
        file_id=_str(obj.get("file_id")),
        width=obj.get("video_width") if isinstance(obj.get("video_width"), int) else None,
        height=obj.get("video_height") if isinstance(obj.get("video_height"), int) else None,
        extra=sanitize_extra(obj, _V1_KNOWN, secrets),
        request_id=request_id,
    )


def parse_retrieve(body: bytes, secrets: list[str] | None = None) -> str:
    obj = _json(body)
    if not isinstance(obj, dict):
        raise VideoError("bad_response", "响应不是对象")
    _check_base_resp(obj, secrets)
    url = _str((obj.get("file") or {}).get("download_url")) if isinstance(obj.get("file"), dict) else None
    if not url:
        raise VideoError("bad_response", "files/retrieve 响应缺少 file.download_url")
    return url


def failure_of(state: TaskState, secrets: list[str] | None = None) -> VideoError:
    """终态不是 succeeded 的任务 → VideoError：审核类（错误码 1026 / 1027 或信息含 sensitive）为 moderation，账号类为 account，其余 task_failed。"""
    code = state.error_code
    message = state.error_message or f"任务状态 {state.status}"
    text = scrub(f"status={state.status} code={code} {message}", secrets)[:400]
    if code in ACCOUNT_CODES:
        return VideoError("account", text, api_code=code, transient=False)
    if code in MODERATION_CODES or "sensitive" in message.lower():
        return VideoError("moderation", text, api_code=code, transient=False)
    return VideoError("task_failed", text, api_code=code, transient=False)


def _header(resp: Response, *names: str) -> str | None:
    for n in names:
        if resp.headers.get(n):
            return resp.headers[n]
    return None


# ---- 客户端 ----


class Client:
    """MiniMax 视频客户端。transport / download_transport 可注入（单测用假 transport）；Key 在发请求时才读取。"""

    provider = PROVIDER

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        transport: Transport | None = None,
        download_transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        download_timeout: float = DOWNLOAD_TIMEOUT,
        base_url: str = BASE_URL,
    ):
        self.env = os.environ if env is None else env
        self.transport = transport or _default_transport
        self.download_transport = download_transport or _download_transport
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.base_url = base_url.rstrip("/")

    def _secrets(self) -> list[str]:
        return providers.secret_values(self.env)

    def _key(self) -> str:
        key = (self.env.get(KEY_ENV) or "").strip()
        if not key:
            raise VideoError("config", f"缺少 {KEY_ENV}", transient=False)
        return key

    def _send(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Response:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._key()}", "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            resp = self.transport(request, self.timeout)
        except VideoError as exc:
            exc.args = (scrub(str(exc), self._secrets()),)
            raise
        if resp.status != 200:
            raise classify_http(resp.status, resp.body, self._secrets())
        return resp

    def endpoint(self, request: VideoRequest) -> tuple[str, str]:
        """创建任务的 (方法, 路径)。"""
        return request.endpoint

    def submit(self, request: VideoRequest) -> Submitted:
        """创建任务。任何发送前都先本地校验；校验不通过抛 VideoError("config")，不发请求。"""
        if not isinstance(request, VideoRequest):
            raise _config_error("submit 只接受 VideoRequest")
        request.validate()
        method, path = request.endpoint
        resp = self._send(method, path, request.body())
        return Submitted(parse_submit(resp.body, self._secrets()), _header(resp, "x-request-id", "trace-id", "x-trace-id"))

    def submit_raw(self, method: str, path: str, body: Mapping[str, Any]) -> Submitted:
        """创建任务（P0-09：参考音频 / 参考视频等 VideoRequest 不覆盖的 content 组合）。调用方必须先做本地校验，这里不再校验。"""
        resp = self._send(method, path, body)
        return Submitted(parse_submit(resp.body, self._secrets()), _header(resp, "x-request-id", "trace-id", "x-trace-id"))

    def query(self, task_id: str, api: str) -> TaskState:
        """查询任务（免费）。api = v2 / v1。"""
        if not _TASK_ID.match(str(task_id)):
            raise _config_error(f"task_id 格式异常：{str(task_id)[:40]!r}")
        path = f"/v2/query/video_generation/{task_id}" if api == "v2" else f"/v1/query/video_generation?task_id={task_id}"
        resp = self._send("GET", path)
        rid = _header(resp, "x-request-id", "trace-id", "x-trace-id")
        parse = parse_task_v2 if api == "v2" else parse_task_v1
        return parse(resp.body, str(task_id), self._secrets(), rid)

    def download_url(self, state: TaskState, api: str) -> str:
        """成功任务的下载地址：v2 直接取 content.url；v1 先调 files/retrieve（免费）。"""
        if api == "v2":
            if not state.video_url:
                raise VideoError("bad_response", "任务成功但响应里没有 content.url")
            return state.video_url
        if not state.file_id:
            raise VideoError("bad_response", "任务成功但响应里没有 file_id")
        if not _TASK_ID.match(state.file_id):
            raise VideoError("bad_response", "file_id 格式异常")
        return parse_retrieve(self._send("GET", f"/v1/files/retrieve?file_id={state.file_id}").body, self._secrets())

    def download(self, url: str) -> bytes:
        return download_bytes(url, self.download_transport, self.download_timeout, self._secrets())


def download_bytes(url: str, transport: Transport, timeout: float, secrets: list[str] | None = None) -> bytes:
    """下载视频：请求不带 Authorization；失败统一为 kind=download，消息带主机名、不带 URL 查询串。"""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise VideoError("download", f"下载地址不是 http(s) URL（host={host}）", transient=False)
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        resp = transport(request, timeout)
    except VideoError as exc:
        raise VideoError("download", scrub(f"{exc} host={host}", secrets), transient=True) from None
    if resp.status != 200:
        raise VideoError("download", f"HTTP {resp.status} host={host}", http_status=resp.status, transient=resp.status >= 500 or resp.status == 429)
    if not resp.body:
        raise VideoError("download", f"下载内容为空 host={host}", transient=True)
    if len(resp.body) > MAX_DOWNLOAD_BYTES:
        raise VideoError("download", f"下载内容超过 {MAX_DOWNLOAD_BYTES} 字节 host={host}", transient=False)
    return resp.body
