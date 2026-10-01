"""火山方舟 Seedance 视频客户端：图生视频（首帧），创建 / 查询 / 下载与本地请求校验（只用标准库，P0-08）。

公共层（VideoError、Response / Transport、TaskState、scrub、下载）复用 poc/minimax.py；接口形态与 Seedream 客户端一致（seedream.py）。

文档（docs.volcengine.com，2026-09-30 用文档页的 getDocDetail JSON 读取，文档更新时间 2026-09-22）：
- 创建视频生成任务 82379/1520757：`POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks`，
  体 {model, content[{type:text,text}, {type:image_url,image_url:{url},role:first_frame}], resolution, ratio, duration, watermark, …}；
  返回 {"id": 任务 ID}（任务记录只保存 7 天）。首帧图：URL / `data:image/<小写格式>;base64,…` / `asset://<素材 ID>`；单张 <30 MB、
  宽高 300–6000 px、宽高比 0.4–2.5，请求体 ≤64 MB；图生视频-首帧 role 为 first_frame 或不填（1 张）。
  参数：resolution（1.0 系列 480p / 720p / 1080p，默认 1080p）、ratio（首帧任务可取 adaptive：按首帧图自动适配，1.0 图生视频默认 adaptive）、
  duration（1.0 系列 [2, 12]，2.0 系列 [4, 15] 或 -1，2.5 [4, 30] 或 -1；-1 由模型自选，与计费相关，本项目不用）、
  watermark（默认 false）、return_last_frame、execution_expires_after（[3600, 259200] 秒，默认 172800）、service_tier（仅 1.0 系列：
  default 在线 / flex 离线半价）、generate_audio（仅 2.5 / 2.0 系列，默认 true，生成同步音频；1.0 系列没有这个参数，产物无声）、
  seed / camera_fixed（仅 1.0 系列）。参数也可用提示词后缀 `--rs 720p` 之类弱校验方式传，本项目不用。
- 查询视频生成任务 82379/1521309：`GET …/contents/generations/tasks/{id}` → {id, model, status, content{video_url, last_frame_url},
  error{code, message}, usage{completion_tokens, total_tokens}, duration（= 实际总帧数 / 24 向下取整）, framespersecond,
  resolution, ratio, generate_audio, service_tier, execution_expires_after, created_at, updated_at, …}；status：queued / running /
  succeeded / failed / cancelled，另有 expired（超过 execution_expires_after）。视频 URL 24 小时有效（2.5 另有 100 次下载上限）。
- 查询任务列表 82379/1521675：`GET …/contents/generations/tasks?page_num=1&page_size=1` → {items, total}；只读、免费，见 probe_list。
- 模型 ID（模型列表 82379/1330310）：doubao-seedance-1-0-pro-fast-251015（首帧 / 文生视频，2~12 秒，480p–1080p，24 fps，mp4，
  “即将下线”）、doubao-seedance-1-0-pro-250528（多首尾帧，即将下线）、doubao-seedance-2-0-260128、doubao-seedance-2-0-fast-260128、
  doubao-seedance-2-0-mini-260615、doubao-seedance-2-5-260628。1.0 系列限流 600 RPM / 并发 10（企业，文档未区分个人）；2.x 个人用户 180 RPM / 并发 3。
- 计费（模型价格 82379/1544106）：token 单价 × usage.completion_tokens，估算 (输入视频时长 + 输出视频时长) × 宽 × 高 × 24 / 1024；
  1.0 pro fast 在线 ¥4.2 / 百万 token（离线 ¥2.1），1.0 pro ¥15，2.0 fast ¥37、mini ¥23、2.0 ¥46（480p / 720p）、2.5 ¥70（480p / 720p）；
  只对成功生成的视频计费，审核失败不收费。单价表见 pricing.VIDEO_PRICES。
- Agent Plan（套餐概览 82379/2366394）：视频生成只在 Large / Max 档，模型为 doubao-seedance-2.0 / 2.0-fast / 2.0-mini / 2.5；
  **Seedance 1.0 系列不在套餐内**（所以 pro fast 只能走按量付费 ARK_BILLING=payg 与按量付费 Key）；视频没有 5 小时 / 周额度，只受日额度（月额度一半）
  与月额度限制；扣点规则见另一页（套餐内 AFP 抵扣规则，本次未读）。
- 人脸限制：2.5 / 2.0 系列不支持直接上传含真人人脸的参考图 / 视频，可用本账号 30 天内 Seedance 2.x / Seedream 5.0 的含人脸原始产物（文生图原始产物）。

未核实（不要当事实用）：
- plan 路径 `/api/plan/v3/contents/generations/tasks` 只来自 P0-06 的笔记与公开资料，文档页没有写视频路径；plan 下是否支持任务列表 GET 未知；
- 上述人脸限制是否适用于“首帧”入参、是否适用于 1.0 系列、首帧（Seedream 5.0 图生图产物，不是文生图原始产物）会不会被拒：文档没有说，要靠冒烟；
- 账号当前是否开通 1.0 pro fast / 2.x（2026-09-30 早先冒烟 2.5 / 2.0 / fast / mini 返回 ModelNotOpen，1.0 系列没有试过）；
- 错误码表（error-codes 页）没有读，账号级 / 审核类错误码沿用 seedream.FATAL_CODES 与审核标记，实际取值以冒烟为准；
- 1.0 系列单价只读到“在线 ¥4.2 / 百万 token”，实际 usage.completion_tokens 与公式是否一致待核对（2.0 480p 公式已与文档示例 ¥2.31 对上）。

请求在创建前都经 VideoRequest 的本地校验（构造时校验、提交前再校验一次）；校验不通过不发请求。下载请求不带 Authorization 头，记录里只有主机名。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request

from poc import images, providers, seedream
from poc.minimax import (
    DEFAULT_TIMEOUT,
    DOWNLOAD_TIMEOUT,
    USER_AGENT,
    Response,
    Submitted,
    TaskState,
    Transport,
    VideoError,
    _default_transport,
    _download_transport,
    _header,
    _json,
    _normalize_status,
    _str,
    _usage,
    data_uri,
    download_bytes,
    sanitize_extra,
    scrub,
)

BASE_URL = seedream.BASE_URL
KEY_ENV = seedream.KEY_ENV
BILLING_ENV = seedream.BILLING_ENV
PROVIDER = seedream.PROVIDER  # ark
TASK_PATHS = {"plan": "/api/plan/v3/contents/generations/tasks", "payg": "/api/v3/contents/generations/tasks"}

MODELS = {
    "1.0-pro-fast": "doubao-seedance-1-0-pro-fast-251015",
    "1.0-pro": "doubao-seedance-1-0-pro-250528",
    "2.0-fast": "doubao-seedance-2-0-fast-260128",
    "2.0-mini": "doubao-seedance-2-0-mini-260615",
    "2.0": "doubao-seedance-2-0-260128",
    "2.5": "doubao-seedance-2-5-260628",
}
DEFAULT_MODEL = "1.0-pro-fast"
# Agent Plan（Large / Max 档）支持的视频模型；1.0 系列不在套餐内（文档 82379/2366394）
PLAN_MODELS = {MODELS["2.0"], MODELS["2.0-fast"], MODELS["2.0-mini"], MODELS["2.5"]}
# Agent Plan 文档（82379/2366394）里的视频模型名是不带日期的别名；带日期的 ID 在套餐路径返回 404 UnsupportedModel（2026-10-01 冒烟）
PLAN_ALIASES = {
    MODELS["2.0"]: "doubao-seedance-2.0",
    MODELS["2.0-fast"]: "doubao-seedance-2.0-fast",
    MODELS["2.0-mini"]: "doubao-seedance-2.0-mini",
    MODELS["2.5"]: "doubao-seedance-2.5",
}
EXPIRES_AFTER_S = 3600  # execution_expires_after 取允许范围的下限：崩溃或超时后的任务尽快过期，不在队列里留 48 小时


@dataclass(frozen=True)
class ModelSpec:
    resolutions: tuple[str, ...]
    durations: tuple[int, ...]
    audio: bool  # 支持 generate_audio
    # 分辨率 → 9:16 输出的宽高（文档“不同宽高比对应的宽高像素值”），只用于 token 估算
    sizes_9x16: Mapping[str, tuple[int, int]]


_S10 = {"480p": (480, 864), "720p": (704, 1248), "1080p": (1088, 1920)}
_S20 = {"480p": (496, 864), "720p": (720, 1280), "1080p": (1080, 1920), "4k": (2160, 3840)}
_S25 = {"480p": (480, 854), "720p": (720, 1280), "1080p": (1080, 1920)}
SPECS: dict[str, ModelSpec] = {
    MODELS["1.0-pro-fast"]: ModelSpec(("480p", "720p", "1080p"), tuple(range(2, 13)), False, _S10),
    MODELS["1.0-pro"]: ModelSpec(("480p", "720p", "1080p"), tuple(range(2, 13)), False, _S10),
    MODELS["2.0-fast"]: ModelSpec(("480p", "720p"), tuple(range(4, 16)), True, _S20),
    MODELS["2.0-mini"]: ModelSpec(("480p", "720p"), tuple(range(4, 16)), True, _S20),
    MODELS["2.0"]: ModelSpec(("480p", "720p", "1080p", "4k"), tuple(range(4, 16)), True, _S20),
    MODELS["2.5"]: ModelSpec(("480p", "720p", "1080p"), tuple(range(4, 31)), True, _S25),
}
IMAGE_MAX_BYTES = 30 * 1024 * 1024
IMAGE_MIN_SIDE, IMAGE_MAX_SIDE = 300, 6000
IMAGE_MIN_RATIO, IMAGE_MAX_RATIO = 0.4, 2.5
BODY_MAX_BYTES = 64 * 1024 * 1024
PROMPT_SANITY_MAX = 4000  # 文档只建议中文不超过 500 字，没有写硬上限；这里只拦明显异常的输入

_MODERATION_MARKERS = ("SensitiveContent", "RiskDetection", "ContentFilter")
_TRANSIENT_CODES = ("ServerOverloaded", "InternalServiceError", "RateLimitExceeded", "QuotaExceeded.Concurrency")
_TASK_ID = re.compile(r"^[0-9A-Za-z_.-]{1,128}$")
FACE_NOTE = (
    "文档：Seedance 2.5 / 2.0 系列不支持直接上传含真人人脸的参考图 / 视频；该限制是否适用于首帧、是否适用于 1.0 系列未核实，"
    "首帧是 Seedream 5.0 pro 图生图产物（不是文生图原始产物），可能被拒，以冒烟为准"
)


def spec_of(model: str) -> ModelSpec:
    try:
        return SPECS[model]
    except KeyError:
        raise VideoError("config", f"未知的 Seedance 模型 {model!r}（可选 {', '.join(SPECS)}）", transient=False) from None


def allowed_durations(model: str, resolution: str) -> tuple[int, ...]:
    spec = spec_of(model)
    if resolution not in spec.resolutions:
        raise VideoError("config", f"{model} 不支持分辨率 {resolution!r}（可选 {' / '.join(spec.resolutions)}）", transient=False)
    return spec.durations


def output_pixels(model: str, resolution: str) -> int:
    """9:16 输出的像素数，用于 token 估算。"""
    w, h = spec_of(model).sizes_9x16[resolution]
    return w * h


def billing_mode(env: Mapping[str, str] | None = None) -> str:
    try:
        return seedream.billing_mode(env)
    except seedream.ImageError as exc:
        raise VideoError("config", str(exc), transient=False) from None


def check_model(model: str, billing: str) -> None:
    """plan 模式只允许套餐内的视频模型（2.x，Large / Max 档）；1.0 系列要走 ARK_BILLING=payg。"""
    spec_of(model)
    if billing == "plan" and model not in PLAN_MODELS:
        raise VideoError(
            "config",
            f"Agent Plan 不含 {model}（套餐内视频模型：{', '.join(sorted(PLAN_MODELS))}，Large / Max 档）；1.0 系列请设 {BILLING_ENV}=payg 并换用按量付费 Key",
            transient=False,
        )


def _config_error(message: str) -> VideoError:
    return VideoError("config", message, transient=False)


def validate_request(
    model: str, resolution: str, duration: int, prompt: str, image: bytes, generate_audio: bool | None = None
) -> images.ImageInfo:
    """本地校验（不发请求）；不合法抛 VideoError("config")，合法返回首帧的 ImageInfo。"""
    spec = spec_of(model)
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise _config_error(f"duration 必须是整数秒（本项目不用 -1 智能时长，费用不可控）：{duration!r}")
    allowed = allowed_durations(model, resolution)
    if duration not in allowed:
        raise _config_error(f"{model} 的 duration 只能取 {allowed[0]}–{allowed[-1]}，收到 {duration}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _config_error("提示词不能为空")
    if len(prompt) > PROMPT_SANITY_MAX:
        raise _config_error(f"提示词 {len(prompt)} 字符，超过本地上限 {PROMPT_SANITY_MAX}（文档建议中文不超过 500 字）")
    if generate_audio is not None and not spec.audio:
        raise _config_error(f"{model} 不支持 generate_audio（只有 2.0 / 2.5 系列支持）")
    try:
        info = images.image_info(image)
    except images.ImageFormatError as exc:
        raise _config_error(f"首帧不是有效的 JPEG / PNG：{exc}") from None
    if info.bytes >= IMAGE_MAX_BYTES:
        raise _config_error(f"首帧 {info.bytes} 字节，须小于 {IMAGE_MAX_BYTES}")
    for name, side in (("宽", info.width), ("高", info.height)):
        if not IMAGE_MIN_SIDE <= side <= IMAGE_MAX_SIDE:
            raise _config_error(f"首帧{name} {side}px 不在 {IMAGE_MIN_SIDE}–{IMAGE_MAX_SIDE} 内")
    ratio = info.width / info.height
    if not IMAGE_MIN_RATIO <= ratio <= IMAGE_MAX_RATIO:
        raise _config_error(f"首帧宽高比 {ratio:.3f} 不在 {IMAGE_MIN_RATIO}–{IMAGE_MAX_RATIO} 内")
    if info.bytes * 4 // 3 + len(prompt.encode("utf-8")) + 4096 > BODY_MAX_BYTES:
        raise _config_error("请求体估算超过 64 MB")
    return info


@dataclass
class VideoRequest:
    """一个 Seedance 图生视频（首帧）请求。构造时本地校验；Client.submit 发送前再校验一次。"""

    model: str
    resolution: str
    duration: int
    prompt: str
    image: bytes = field(repr=False)
    generate_audio: bool | None = None  # None = 不传（服务端默认 true，2.x 会带音频）；1.0 系列必须为 None
    watermark: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> images.ImageInfo:
        return validate_request(self.model, self.resolution, self.duration, self.prompt, self.image, self.generate_audio)

    api = "ark"

    @property
    def ratio(self) -> str:
        return "adaptive"  # 首帧任务：宽高比随首帧图（2.5 只支持 adaptive）

    def body(self, image_uri: str | None = None, billing: str = "payg") -> dict[str, Any]:
        info = images.image_info(self.image)
        uri = image_uri if image_uri is not None else data_uri(self.image, info.fmt)
        body: dict[str, Any] = {
            "model": PLAN_ALIASES.get(self.model, self.model) if billing == "plan" else self.model,
            "content": [
                {"type": "text", "text": self.prompt},
                {"type": "image_url", "image_url": {"url": uri}, "role": "first_frame"},
            ],
            "resolution": self.resolution,
            "ratio": self.ratio,
            "duration": self.duration,
            "watermark": self.watermark,
            "execution_expires_after": EXPIRES_AFTER_S,
        }
        if self.generate_audio is not None:
            body["generate_audio"] = self.generate_audio
        return body

    def preview(self) -> dict[str, Any]:
        info = images.image_info(self.image)
        return self.body(f"<data:image/{info.fmt};base64, {info.bytes} bytes, sha256={info.sha256}>")


# ---- 响应解析与分类 ----

_KNOWN = {"id", "model", "status", "content", "created_at", "updated_at", "duration", "frames", "error", "usage", "resolution", "ratio"}


def _error_of(obj: Any) -> tuple[str | None, str]:
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = obj["error"]
        return _str(err.get("code")), str(err.get("message") or "")[:300]
    return None, ""


_ACCOUNT_RE = re.compile(r"(account\s+)\d+", re.IGNORECASE)


def _scrub(text: str, secrets: list[str] | None) -> str:
    """脱敏：密钥值、URL 查询串，以及错误信息里的方舟账号 ID。"""
    return _ACCOUNT_RE.sub(r"\1***", scrub(text, secrets))


def classify(status: int, code: str | None, message: str, secrets: list[str] | None = None) -> VideoError:
    text = _scrub(f"HTTP {status} code={code} {message}".strip(), secrets)[:400]
    c = code or ""
    if status == 401 or c in seedream.FATAL_CODES:
        return VideoError("account", text, http_status=status, api_code=code, transient=False)
    if any(m in c for m in _MODERATION_MARKERS):
        return VideoError("moderation", text, http_status=status, api_code=code, transient=False)
    if status == 429 or any(t in c for t in _TRANSIENT_CODES):
        return VideoError("rate_limit" if status == 429 or "RateLimit" in c or "Quota" in c else "http", text, http_status=status, api_code=code, transient=True)
    return VideoError("http", text, http_status=status, api_code=code, transient=status >= 500)


def classify_http(status: int, body: bytes, secrets: list[str] | None = None) -> VideoError:
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        obj = None
    code, message = _error_of(obj)
    if not code and not message:
        message = body[:200].decode("utf-8", "replace")
    return classify(status, code, message, secrets)


def parse_submit(body: bytes, secrets: list[str] | None = None) -> str:
    obj = _json(body)
    if not isinstance(obj, dict):
        raise VideoError("bad_response", "响应不是对象")
    code, message = _error_of(obj)
    if code:
        raise classify(200, code, message, secrets)
    task_id = _str(obj.get("id"))
    if task_id is None:
        raise VideoError("bad_response", "响应缺少 id")
    if not _TASK_ID.match(task_id):
        raise VideoError("bad_response", "任务 ID 格式异常")
    return task_id


def parse_task(body: bytes, task_id: str, secrets: list[str] | None = None, request_id: str | None = None) -> TaskState:
    """查询响应：任务字段平铺；也兼容 {"items": [...]}（列表形态）与 {"task": {...}}。"""
    obj = _json(body)
    if not isinstance(obj, dict):
        raise VideoError("bad_response", "响应不是对象")
    task: Mapping[str, Any] = obj
    if isinstance(obj.get("task"), dict):
        task = obj["task"]
    elif isinstance(obj.get("items"), list):
        dicts = [i for i in obj["items"] if isinstance(i, dict)]
        match = [i for i in dicts if str(i.get("id")) == task_id] or (dicts if len(dicts) == 1 else [])
        if not match:
            raise VideoError("bad_response", f"查询结果里没有任务 {task_id}")
        task = match[0]
    content = task.get("content") if isinstance(task.get("content"), dict) else {}
    error = task.get("error") if isinstance(task.get("error"), dict) else {}
    extra = sanitize_extra(task, _KNOWN, secrets)
    for key, text in sanitize_extra(content, {"video_url", "last_frame_url"}, secrets).items():
        extra[f"content.{key}"] = text
    return TaskState(
        task_id=_str(task.get("id")) or task_id,
        status=_normalize_status(task.get("status")),
        model=_str(task.get("model")),
        video_url=_str(content.get("video_url")),
        error_code=_str(error.get("code")),
        error_message=_scrub(str(error.get("message") or ""), secrets)[:300] or None,
        resolution=_str(task.get("resolution")),
        duration=task.get("duration") if isinstance(task.get("duration"), int) else None,
        ratio=_str(task.get("ratio")),
        usage=_usage(task.get("usage")),
        created_at=task.get("created_at") if isinstance(task.get("created_at"), int) else None,
        updated_at=task.get("updated_at") if isinstance(task.get("updated_at"), int) else None,
        extra=extra,
        request_id=request_id,
    )


def failure_of(state: TaskState, secrets: list[str] | None = None) -> VideoError:
    """终态不是 succeeded 的任务 → VideoError：审核类为 moderation，账号类为 account，其余（含 expired）task_failed。"""
    code = state.error_code or ""
    message = state.error_message or f"任务状态 {state.status}"
    text = _scrub(f"status={state.status} code={state.error_code} {message}", secrets)[:400]
    if code in seedream.FATAL_CODES:
        return VideoError("account", text, api_code=state.error_code, transient=False)
    if any(m in code for m in _MODERATION_MARKERS):
        return VideoError("moderation", text, api_code=state.error_code, transient=False)
    return VideoError("task_failed", text, api_code=state.error_code, transient=False)


# ---- 客户端 ----


class Client:
    """方舟 Seedance 客户端。transport / download_transport 可注入；Key 与 ARK_BILLING 在发请求时才读取。"""

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

    @property
    def billing(self) -> str:
        return billing_mode(self.env)

    @property
    def tasks_path(self) -> str:
        return TASK_PATHS[self.billing]

    def _send(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Response:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._key()}", "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            resp = self.transport(request, self.timeout)
        except VideoError as exc:
            exc.args = (_scrub(str(exc), self._secrets()),)
            raise
        if resp.status != 200:
            raise classify_http(resp.status, resp.body, self._secrets())
        return resp

    def endpoint(self, request: VideoRequest) -> tuple[str, str]:
        """创建任务的 (方法, 路径)；路径由 ARK_BILLING 决定。"""
        return ("POST", TASK_PATHS[self.billing])

    def submit(self, request: VideoRequest) -> Submitted:
        """创建任务。先本地校验（含 ARK_BILLING 与模型是否匹配）；不通过抛 VideoError("config")，不发请求。"""
        if not isinstance(request, VideoRequest):
            raise _config_error("submit 只接受 VideoRequest")
        request.validate()
        billing = self.billing
        check_model(request.model, billing)
        resp = self._send("POST", TASK_PATHS[billing], request.body(billing=billing))
        return Submitted(parse_submit(resp.body, self._secrets()), _header(resp, "x-request-id", "x-tt-logid"))

    def query(self, task_id: str, api: str = "ark") -> TaskState:
        if not _TASK_ID.match(str(task_id)):
            raise _config_error(f"task_id 格式异常：{str(task_id)[:40]!r}")
        resp = self._send("GET", f"{self.tasks_path}/{task_id}")
        return parse_task(resp.body, str(task_id), self._secrets(), _header(resp, "x-request-id", "x-tt-logid"))

    def download_url(self, state: TaskState, api: str = "ark") -> str:
        if not state.video_url:
            raise VideoError("bad_response", "任务成功但响应里没有 content.video_url")
        return state.video_url

    def download(self, url: str) -> bytes:
        return download_bytes(url, self.download_transport, self.download_timeout, self._secrets())

    def probe_list(self) -> dict[str, Any]:
        """免费、只读的 GET 探测：查询最近的 1 条任务（文档 82379/1521675）。不创建任务、不计费。

        用来确认 Key / 路径 / 账号是否可用于视频接口（不能确认模型是否开通：开通状态只在创建任务时校验）。
        返回 {"ok", "billing", "path", "total", "items"}；失败抛 VideoError（401 → account）。
        """
        billing = self.billing
        path = f"{TASK_PATHS[billing]}?page_num=1&page_size=1"
        resp = self._send("GET", path)
        obj = _json(resp.body)
        if not isinstance(obj, dict):
            raise VideoError("bad_response", "响应不是对象")
        items = obj.get("items") if isinstance(obj.get("items"), list) else []
        return {"ok": True, "billing": billing, "path": TASK_PATHS[billing], "total": obj.get("total"), "items": len(items)}
