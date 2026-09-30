"""豆包语音（火山引擎）客户端：TTS 2.0 与录音文件识别 2.0 标准版（只用标准库，P0-05）。

鉴权：新版控制台 API Key，请求头 `x-api-key`；`X-Api-Resource-Id` 选择模型并决定计费商品。
不跟随重定向（沿用 doctor 的 opener），以免鉴权头被带到其他主机。

TTS  POST /api/v3/tts/unidirectional（resource seed-tts-2.0）
     响应为逐行 JSON：{"code":0,"data":"<base64 音频片段>"} …，结束块 {"code":20000000,"usage":{"text_words":N}}。
     必须收到结束块才算成功；带 X-Control-Require-Usage-Tokens-Return 头时结束块返回计费字符数。
ASR  POST /api/v3/auc/bigmodel/submit → POST /api/v3/auc/bigmodel/query 轮询（resource volc.seedasr.auc）
     状态在响应头 X-Api-Status-Code：20000000 完成，20000001 处理中，20000002 排队，20000003 静音音频。
     音频以 base64 内联在 audio.data 中（实测可用），不需要公网 URL。

失败统一抛出 SpeechError，kind 标明原因：
- network      DNS、连接、超时、代理拒绝、传输中途断开
- http         HTTP 4xx / 5xx
- api_error    HTTP 200 但业务状态码表示失败
- truncated    TTS 流没有结束块
- empty        TTS 结束但没有音频
- bad_response 响应不是预期结构
- timeout      ASR 轮询超时
transient 属性标明是否可重试（网络、截断、429、5xx、服务端繁忙 / 并发限流）。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from poc import config, doctor, providers

BASE_URL = "https://openspeech.bytedance.com"
TTS_PATH = "/api/v3/tts/unidirectional"
ASR_SUBMIT_PATH = "/api/v3/auc/bigmodel/submit"
ASR_QUERY_PATH = "/api/v3/auc/bigmodel/query"
KEY_ENV = "VOLC_SPEECH_API_KEY"
PROVIDER = "volc_speech"
TTS_RESOURCE = "seed-tts-2.0"
ASR_RESOURCE = "volc.seedasr.auc"
USER_AGENT = "dramio-poc-speech/0"
DEFAULT_TIMEOUT = 60.0

TTS_DONE = 20000000
ASR_DONE = 20000000
ASR_PENDING = (20000001, 20000002)
ASR_SILENT = 20000003
_TRANSIENT_API_CODES = {55000000, 55000031}


class SpeechError(Exception):
    def __init__(
        self, kind: str, message: str, http_status: int | None = None, api_code: int | None = None, transient: bool | None = None
    ):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.http_status = http_status
        self.api_code = api_code
        if transient is None:
            transient = (
                kind in ("network", "truncated")
                or (kind == "http" and (http_status == 429 or (http_status or 0) >= 500))
                or (api_code in _TRANSIENT_API_CODES)
                or ("concurrency" in message)
            )
        self.transient = transient


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # 键为小写
    body: bytes


# transport(request, timeout) -> Response；HTTP 错误也返回 Response，网络错误抛 SpeechError。测试时注入假实现
Transport = Callable[[Request, float], Response]


def _default_transport(request: Request, timeout: float) -> Response:
    try:
        with doctor.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, body)
    except HTTPError as exc:
        try:
            body = exc.read(4096)
        except Exception:
            body = b""
        return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
    except (URLError, OSError, http.client.HTTPException) as exc:  # 含超时、connection reset、IncompleteRead
        reason = getattr(exc, "reason", exc)
        raise SpeechError("network", f"{type(exc).__name__}: {reason}") from None


def _key(env: Mapping[str, str]) -> str:
    key = (env.get(KEY_ENV) or "").strip()
    if not key:
        raise SpeechError("config", f"缺少 {KEY_ENV}", transient=False)
    return key


def _headers(key: str, resource: str, **extra: str) -> dict[str, str]:
    return {
        "x-api-key": key,
        "X-Api-Resource-Id": resource,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        **extra,
    }


def _send(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float, transport: Transport | None, secrets) -> Response:
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        return (transport or _default_transport)(request, timeout)
    except SpeechError as exc:
        exc.args = (config.redact(str(exc), secrets),)
        raise


def _api_code_from_body(body: bytes) -> tuple[int | None, str]:
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None, body[:300].decode("utf-8", "replace")
    if isinstance(obj, dict):
        head = obj.get("header") if isinstance(obj.get("header"), dict) else obj
        code = head.get("code")
        return (code if isinstance(code, int) else None), str(head.get("message") or "")[:300]
    return None, str(obj)[:300]


def _http_error(resp: Response, secrets) -> SpeechError:
    code, message = _api_code_from_body(resp.body)
    if code is None and resp.headers.get("x-api-status-code", "").isdigit():
        code = int(resp.headers["x-api-status-code"])
        message = message or resp.headers.get("x-api-message", "")
    text = config.redact(f"HTTP {resp.status} code={code} {message}".strip(), secrets)
    return SpeechError("http", text, http_status=resp.status, api_code=code)


# ---- TTS ----


@dataclass
class TTSResult:
    audio: bytes
    text_words: int | None  # 供应商返回的计费字符数
    chunks: int
    logid: str | None = None


def build_tts_body(
    text: str,
    speaker: str,
    *,
    speech_rate: int = 0,
    context_text: str | None = None,
    fmt: str = "mp3",
    sample_rate: int = 24000,
    uid: str = "dramio-poc",
) -> dict[str, Any]:
    audio_params: dict[str, Any] = {"format": fmt, "sample_rate": sample_rate}
    if speech_rate:
        audio_params["speech_rate"] = speech_rate
    req: dict[str, Any] = {"text": text, "speaker": speaker, "audio_params": audio_params}
    if context_text:
        # additions 是 JSON 字符串；context_texts 只有第一个元素生效，且不计费
        req["additions"] = json.dumps({"context_texts": [context_text]}, ensure_ascii=False)
    return {"user": {"uid": uid}, "req_params": req}


def parse_tts_stream(body: bytes) -> tuple[bytes, int | None, int]:
    """解析逐行 JSON 流 → (音频, 计费字符数, 音频片段数)。没有结束块抛 truncated。"""
    audio = bytearray()
    chunks = 0
    lines = [ln for ln in body.split(b"\n") if ln.strip()]
    for i, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            if i == len(lines) - 1:
                raise SpeechError("truncated", f"最后一行不是完整 JSON（{len(line)} 字节）") from None
            raise SpeechError("bad_response", f"第 {i + 1} 行不是 JSON") from None
        if not isinstance(obj, dict) or not isinstance(obj.get("code"), int):
            raise SpeechError("bad_response", f"第 {i + 1} 行缺少 code")
        code = obj["code"]
        if code == 0:
            data = obj.get("data")
            if data:
                try:
                    audio += base64.b64decode(data, validate=True)
                except ValueError:
                    raise SpeechError("bad_response", f"第 {i + 1} 行 data 不是 base64") from None
                chunks += 1
            continue
        if code == TTS_DONE:
            if i != len(lines) - 1:
                raise SpeechError("bad_response", "结束块之后还有数据")
            if not audio:
                raise SpeechError("empty", "流已结束但没有音频", transient=False)
            words = (obj.get("usage") or {}).get("text_words")
            return bytes(audio), (words if isinstance(words, int) else None), chunks
        raise SpeechError("api_error", f"code={code} {str(obj.get('message') or '')[:300]}", api_code=code)
    raise SpeechError("truncated", f"没有结束块（已收到 {chunks} 个音频片段）")


def synthesize(
    text: str,
    speaker: str,
    *,
    resource: str = TTS_RESOURCE,
    speech_rate: int = 0,
    context_text: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport | None = None,
) -> TTSResult:
    env = os.environ if env is None else env
    key = _key(env)
    secrets = providers.secret_values(env)
    headers = _headers(key, resource, **{"X-Control-Require-Usage-Tokens-Return": "text_words"})
    body = build_tts_body(text, speaker, speech_rate=speech_rate, context_text=context_text)
    resp = _send(BASE_URL + TTS_PATH, headers, body, timeout, transport, secrets)
    if resp.status != 200:
        raise _http_error(resp, secrets)
    audio, words, chunks = parse_tts_stream(resp.body)
    return TTSResult(audio, words, chunks, resp.headers.get("x-tt-logid"))


# ---- ASR ----


@dataclass
class ASRResult:
    text: str
    utterances: list[dict[str, Any]]  # 每项 text、start_ms、end_ms
    duration_ms: int | None
    silent: bool
    request_id: str
    polls: int
    raw_status: int
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "resource_id": ASR_RESOURCE,
            "text": self.text,
            "duration_ms": self.duration_ms,
            "silent": self.silent,
            "utterances": self.utterances,
        }


def _status(resp: Response) -> tuple[int | None, str]:
    raw = resp.headers.get("x-api-status-code", "")
    return (int(raw) if raw.isdigit() else None), resp.headers.get("x-api-message", "")


def asr_submit(audio: bytes, *, fmt: str = "mp3", request_id: str | None = None, env=None, timeout=DEFAULT_TIMEOUT, transport=None) -> str:
    env = os.environ if env is None else env
    key = _key(env)
    secrets = providers.secret_values(env)
    request_id = request_id or str(uuid.uuid4())
    body = {
        "user": {"uid": "dramio-poc"},
        "audio": {"data": base64.b64encode(audio).decode("ascii"), "format": fmt},
        # enable_itn 关闭：保留汉字数字，避免“三”与“3”之类的差异抬高 CER
        "request": {"model_name": "bigmodel", "enable_itn": False, "enable_punc": True, "show_utterances": True},
    }
    headers = _headers(key, ASR_RESOURCE, **{"X-Api-Request-Id": request_id, "X-Api-Sequence": "-1"})
    resp = _send(BASE_URL + ASR_SUBMIT_PATH, headers, body, timeout, transport, secrets)
    if resp.status != 200:
        raise _http_error(resp, secrets)
    code, message = _status(resp)
    if code != ASR_DONE:
        raise SpeechError("api_error", config.redact(f"submit code={code} {message}", secrets), api_code=code)
    return request_id


def asr_query(request_id: str, *, env=None, timeout=DEFAULT_TIMEOUT, transport=None) -> tuple[int, dict[str, Any] | None]:
    """查询一次 → (状态码, 完成时的响应 JSON)。处理中返回 (2000000x, None)。"""
    env = os.environ if env is None else env
    key = _key(env)
    secrets = providers.secret_values(env)
    headers = _headers(key, ASR_RESOURCE, **{"X-Api-Request-Id": request_id})
    resp = _send(BASE_URL + ASR_QUERY_PATH, headers, {}, timeout, transport, secrets)
    if resp.status != 200:
        raise _http_error(resp, secrets)
    code, message = _status(resp)
    if code in ASR_PENDING:
        return code, None
    if code == ASR_SILENT:
        return code, {}
    if code != ASR_DONE:
        raise SpeechError("api_error", config.redact(f"query code={code} {message}", secrets), api_code=code)
    try:
        obj = json.loads(resp.body.decode("utf-8"))
    except ValueError:
        raise SpeechError("bad_response", "query 响应不是 JSON") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("result"), dict):
        raise SpeechError("bad_response", "query 响应缺少 result")
    return code, obj


def parse_asr_result(obj: dict[str, Any]) -> tuple[str, list[dict[str, Any]], int | None]:
    result = obj.get("result") or {}
    text = result.get("text") or ""
    utterances = [
        {"text": u.get("text", ""), "start_ms": int(u.get("start_time", 0)), "end_ms": int(u.get("end_time", 0))}
        for u in (result.get("utterances") or [])
        if isinstance(u, dict)
    ]
    duration = (obj.get("audio_info") or {}).get("duration")
    if duration is None:
        duration = (result.get("additions") or {}).get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return text, utterances, duration


def poll_asr(
    request_id: str,
    query: Callable[[str], tuple[int, dict[str, Any] | None]],
    *,
    interval: float = 1.0,
    max_wait: float = 60.0,
    sleep=time.sleep,
    clock=time.monotonic,
) -> ASRResult:
    """轮询到完成、静音或超时。query 由调用方包一层记账。"""
    t0 = clock()
    polls = 0
    while True:
        sleep(interval)
        polls += 1
        code, obj = query(request_id)
        if obj is not None:
            if code == ASR_SILENT:
                return ASRResult("", [], None, True, request_id, polls, code)
            text, utterances, duration = parse_asr_result(obj)
            return ASRResult(text, utterances, duration, False, request_id, polls, code)
        if clock() - t0 >= max_wait:
            raise SpeechError("timeout", f"轮询 {polls} 次、{max_wait:g} 秒仍未完成（最后状态 {code}）", transient=True)
