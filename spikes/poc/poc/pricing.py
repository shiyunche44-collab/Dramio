"""LLM 单价（元 / 百万 tokens），按 usage 估算单次调用费用（cost_basis=estimate）。

来源说明：DeepSeek 官方价格页（api-docs.deepseek.com）被当前云环境的网络策略拦截，仓库内也没有单价资料。
下表是按 DeepSeek 以往公开价量级取的保守上界。P0-03 全部 22 次调用估算 ¥2.44，账户余额实际减少 ¥1.46
（见 docs/reports/p0/P0-03.md），即实际约为估算的 60%。余额只精确到 0.01 元且入账有延迟，只能按批次复核，单次费用仍是估算。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Price:
    input_cache_hit: float
    input_cache_miss: float
    output: float  # 含思维链（reasoning）tokens
    source: str


# 见模块说明
PRICES: dict[tuple[str, str], Price] = {
    ("deepseek", "deepseek-flash"): Price(0.5, 2.0, 8.0, "保守上界，2026-09-30 余额差复核"),
    ("deepseek", "deepseek-v4-pro"): Price(1.0, 4.0, 16.0, "保守上界，2026-09-30 余额差复核"),
}

# 未登记的模型按此保守估算，避免费用保险失效
FALLBACK = Price(4.0, 16.0, 64.0, "未登记模型的保守估算")


def price_for(provider: str, model: str) -> Price:
    return PRICES.get((provider, model), FALLBACK)


def estimate_cny(provider: str, model: str, usage: dict[str, Any] | None) -> float:
    if not usage:
        return 0.0
    p = price_for(provider, model)
    prompt = int(usage.get("prompt_tokens") or 0)
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is None or miss is None:
        hit, miss = 0, prompt  # 没有缓存明细时全部按未命中计
    output = int(usage.get("completion_tokens") or 0)
    cost = (int(hit) * p.input_cache_hit + int(miss) * p.input_cache_miss + output * p.output) / 1_000_000
    return round(cost, 6)


# ---- 语音（P0-05）：按字符 / 按音频时长计价 ----
# 来源：豆包语音计费说明 doc 6561/1359370（2026-09-30 读取）：后付费刊例价，语音合成模型 2.0 ¥3 / 万字符，
# 录音文件识别模型 2.0 ¥0.8 / 小时（预付费资源包更低，P0 按刊例价保守估算）。
# TTS 2.0 按“计费字符数”计费；结束块返回 usage.text_words 时用它，否则按全部字符（含标点）保守计算。
TTS_CNY_PER_10K_CHARS: dict[tuple[str, str], float] = {
    ("volc_speech", "seed-tts-2.0"): 3.0,
}
ASR_CNY_PER_HOUR: dict[tuple[str, str], float] = {
    ("volc_speech", "volc.seedasr.auc"): 0.8,
}
SPEECH_PRICE_SOURCE = "豆包语音计费说明 doc 6561/1359370（后付费刊例价，2026-09-30）"


def tts_cny(provider: str, resource: str, chars: int) -> float:
    return round(chars * TTS_CNY_PER_10K_CHARS[(provider, resource)] / 10_000, 6)


def asr_cny(provider: str, resource: str, seconds: float) -> float:
    return round(seconds * ASR_CNY_PER_HOUR[(provider, resource)] / 3600, 6)


# ---- 音乐（P0-10）：按成功生成的时长（秒）计价 ----
# 来源：豆包音乐产品计费 doc 84992/1404661（2026-10-01 读取）：后付费 ¥0.002 / 秒，按最终成功生成的时长计（示例 200 秒 = ¥0.4）。
# 文档示例价，**未核对账单**（verified=False）；预付费资源包另计，P0 按后付费刊例价保守估算。
MUSIC_CNY_PER_SECOND: dict[tuple[str, str], float] = {
    ("volc_music", "GenBGMForTime"): 0.002,
}
MUSIC_PRICE_SOURCE = "豆包音乐产品计费 doc 84992/1404661（后付费刊例价，2026-10-01，未核对账单）"
MUSIC_PRICE_VERIFIED = False


def music_cny(provider: str, action: str, seconds: float) -> float:
    return round(seconds * MUSIC_CNY_PER_SECOND[(provider, action)], 6)


# ---- 图像（P0-06）：按张计价 ----
# 来源：方舟模型价格 doc 82379/1544106（2026-09-30 读取）：
# Seedream 5.0 pro 单图生成 ≤261 万像素（1.5K 及以下）¥0.30 / 张，>261 万像素 ¥0.60 / 张；参考图首张免费、第 2 张起 ¥0.02 / 张；
# Seedream 5.0 flash ¥0.12 / 张、Seedream 4.0 ¥0.20 / 张，参考图免费。只对成功生成的图片计费（usage.generated_images），审核未通过不计费。
IMAGE_PRICE_SOURCE = "方舟模型价格 doc 82379/1544106（2026-09-30）"
IMAGE_TIER_PIXELS = 2_610_000
# Agent Plan（包月，按 AFP 点数扣额度）的调用不另外付费；cost_cny 仍按上面的按量刊例价记“等价费用”，用于成本模型（D-007）
PLAN_COST_NOTE = "billing=plan：Agent Plan 包月额度内调用，cost_cny 为按量刊例价的等价费用，不实付"


@dataclass(frozen=True)
class ImagePrice:
    low: float  # ≤ IMAGE_TIER_PIXELS
    high: float  # > IMAGE_TIER_PIXELS
    extra_ref: float  # 第 2 张参考图起每张


IMAGE_PRICES: dict[tuple[str, str], ImagePrice] = {
    ("ark", "doubao-seedream-5-0-pro-260628"): ImagePrice(0.30, 0.60, 0.02),
    ("ark", "doubao-seedream-5-0-flash-260915"): ImagePrice(0.12, 0.12, 0.0),
    ("ark", "doubao-seedream-4-0-20260415"): ImagePrice(0.20, 0.20, 0.0),
}
_TIER_PIXELS = {"1k": 1024 * 1024, "1.5k": 1536 * 1536, "2k": 2048 * 2048}


def size_pixels(size: str) -> int:
    """'宽x高' 或档位（1K / 1.5K / 2K，按 1:1 的像素数，只用于判断价格档位）。"""
    w, sep, h = size.lower().partition("x")
    if sep and w.isdigit() and h.isdigit():
        return int(w) * int(h)
    if size.lower() in _TIER_PIXELS:
        return _TIER_PIXELS[size.lower()]
    raise ValueError(f"无法识别的尺寸：{size!r}")


def image_cny(provider: str, model: str, pixels: int, n_refs: int = 0, n_images: int = 1) -> float:
    p = IMAGE_PRICES[(provider, model)]
    per = p.low if pixels <= IMAGE_TIER_PIXELS else p.high
    return round(n_images * per + max(0, n_refs - 1) * p.extra_ref, 6)


# ---- 视频（P0-08）：按秒 / 按视频 / 按 token 三种计价 ----
# 价格表每一项带 verified 与 source。verified=False 表示只来自文档页、尚未被实际调用核对（冒烟实测、usage 与账单对上之后改 True，
# 并在 source 补充核对记录）。2026-09-30 起全部为 False。
# 费用保险用更严的口径 guard_cny：未验证的单价乘 UNVERIFIED_MARGIN，没有登记的（模型 / 分辨率 / 时长不在表里）按 FALLBACK_CNY_PER_SECOND 兜底，
# 二者都只会让 --max-cost-cny 偏严；calls.jsonl 记账与 dry-run 预览用文档单价（cny）。
MINIMAX_PRICE_SOURCE = "MiniMax 按量计费文档页 platform.minimax.cn/docs/guides/pricing-paygo（2026-09-30 读取）"
ARK_VIDEO_PRICE_SOURCE = "方舟模型价格 doc 82379/1544106（2026-09-30 读取，在线推理刊例价，不含限时折扣）"
UNVERIFIED_MARGIN = 1.5
FALLBACK_CNY_PER_SECOND = 4.0  # 高于已知的最高单价（Seedance 2.5 1080p 约 3.7 元 / 秒，Hailuo-2.3 1080P 约 0.58 元 / 秒）
SEEDANCE_FPS = 24
PRICE_UNITS = ("per_second", "per_video", "per_mtoken")


@dataclass(frozen=True)
class VideoPrice:
    unit: str  # per_second 元 / 秒；per_video 元 / 条（按 per_video 的时长查表）；per_mtoken 元 / 百万 token
    cny: float  # per_video 时为 0
    source: str
    verified: bool = False
    per_video: Mapping[int, float] = field(default_factory=dict)  # 时长（秒）→ 元 / 条
    note: str = ""


def _mm(unit_cny: float, note: str = "") -> VideoPrice:
    return VideoPrice("per_second", unit_cny, MINIMAX_PRICE_SOURCE, False, note=note)


VIDEO_PRICES: dict[tuple[str, str, str], VideoPrice] = {
    # MiniMax v2：按输出秒数计费（usage.output_seconds）；首帧图 H3 5 张以内免费、H3-Max 2 张以内免费；失败或命中审核不计费
    ("minimax", "MiniMax-H3", "768P"): _mm(0.50),
    ("minimax", "MiniMax-H3", "2K"): _mm(0.80),
    ("minimax", "MiniMax-H3-Max", "480P"): _mm(0.33),
    ("minimax", "MiniMax-H3-Max", "768P"): _mm(0.50),
    # MiniMax v1 Hailuo-2.3：按条计费，查询响应没有 usage，按请求的时长查表
    ("minimax", "MiniMax-Hailuo-2.3", "768P"): VideoPrice("per_video", 0.0, MINIMAX_PRICE_SOURCE, False, {6: 2.00, 10: 4.00}, "图生视频 768P"),
    ("minimax", "MiniMax-Hailuo-2.3", "1080P"): VideoPrice("per_video", 0.0, MINIMAX_PRICE_SOURCE, False, {6: 3.50}, "图生视频 1080P"),
}
# 方舟 Seedance：视频价格 = token 单价 × 用量，用量以 usage.completion_tokens 为准；
# 估算公式 (输入视频时长 + 输出视频时长) × 宽 × 高 × 帧率 / 1024（本项目无输入视频，帧率 24）。元 / 百万 token（在线推理）；
# 1.0 系列另有离线推理（flex）半价，本项目不用。只对成功生成的视频计费。Agent Plan 调用不另外付费，cny 仍记按量等价费用（同 PLAN_COST_NOTE）。
_ARK_TOKEN_PRICES = {
    "doubao-seedance-1-0-pro-fast-251015": {"480p": 4.2, "720p": 4.2, "1080p": 4.2},
    "doubao-seedance-1-0-pro-250528": {"480p": 15.0, "720p": 15.0, "1080p": 15.0},
    "doubao-seedance-2-0-fast-260128": {"480p": 37.0, "720p": 37.0},
    "doubao-seedance-2-0-mini-260615": {"480p": 23.0, "720p": 23.0},
    "doubao-seedance-2-0-260128": {"480p": 46.0, "720p": 46.0, "1080p": 51.0, "4k": 26.0},
    "doubao-seedance-2-5-260628": {"480p": 70.0, "720p": 70.0, "1080p": 77.0},
}
for _model, _by_res in _ARK_TOKEN_PRICES.items():
    for _res, _cny in _by_res.items():
        VIDEO_PRICES[("ark", _model, _res)] = VideoPrice("per_mtoken", _cny, ARK_VIDEO_PRICE_SOURCE, False, note="输入不含视频、在线推理")
del _model, _by_res, _res, _cny


def video_tokens(seconds: float, pixels: int, fps: int = SEEDANCE_FPS) -> int:
    """Seedance 的 token 用量估算：时长 × 宽 × 高（pixels = 宽 × 高）× 帧率 / 1024（无输入视频）。"""
    return round(seconds * pixels * fps / 1024)


@dataclass(frozen=True)
class VideoCost:
    cny: float  # 按文档单价的费用（记账、预览用）
    guard_cny: float  # 费用保险用：未验证 × UNVERIFIED_MARGIN，未登记按兜底
    unit: str | None  # None 表示单价未登记
    unit_price: float | None
    quantity: float | None  # 计费量：秒 / 条 / token
    quantity_source: str  # usage（供应商返回的用量）/ requested（按请求的时长）/ formula（按公式估算 token）/ fallback
    verified: bool
    known: bool
    source: str
    note: str = ""


def video_cost(
    provider: str,
    model: str,
    resolution: str,
    seconds: float,
    *,
    output_seconds: float | None = None,
    tokens: int | None = None,
    pixels: int | None = None,
) -> VideoCost:
    """一个视频任务的费用。

    seconds：请求的时长；output_seconds：查询响应 usage.output_seconds（实际计费秒数，以它为准，没有则用 seconds 估算）；
    tokens：Seedance 的 usage.completion_tokens；pixels：输出视频宽 × 高（没有 tokens 时按公式估算 token 用）。
    单价没有登记、或缺少估算所需的数据时，按 FALLBACK_CNY_PER_SECOND 兜底（known=False）。
    """
    price = VIDEO_PRICES.get((provider, model, resolution))
    qty_seconds = seconds if output_seconds is None else output_seconds
    second_source = "requested" if output_seconds is None else "usage"

    def fallback(why: str) -> VideoCost:
        cny = round(qty_seconds * FALLBACK_CNY_PER_SECOND, 6)
        return VideoCost(cny, cny, None, None, qty_seconds, "fallback", False, False, "未登记单价", why)

    if price is None:
        return fallback(f"{provider} / {model} / {resolution}")
    if price.unit == "per_second":
        cny = round(qty_seconds * price.cny, 6)
        qty, qsrc = qty_seconds, second_source
    elif price.unit == "per_video":
        flat = price.per_video.get(int(seconds))
        if flat is None:
            return fallback(f"{model} {resolution} 没有 {seconds:g} 秒的单价")
        cny, qty, qsrc = round(flat, 6), 1, "requested"
    elif price.unit == "per_mtoken":
        if tokens is not None:
            qty, qsrc = tokens, "usage"
        elif pixels:
            qty, qsrc = video_tokens(qty_seconds, pixels), "formula"
        else:
            return fallback("缺少 token 用量与输出像素，无法估算")
        cny = round(qty * price.cny / 1_000_000, 6)
    else:
        raise ValueError(f"未知计价单位：{price.unit}")
    guard = cny if price.verified else round(cny * UNVERIFIED_MARGIN, 6)
    return VideoCost(cny, guard, price.unit, price.cny, qty, qsrc, price.verified, True, price.source, price.note)


def video_price_block(provider: str, model: str, resolution: str) -> dict[str, Any]:
    """summary 中的单价说明：单位、单价、verified、来源。"""
    price = VIDEO_PRICES.get((provider, model, resolution))
    if price is None:
        return {"provider": provider, "model": model, "resolution": resolution, "known": False, "fallback_cny_per_second": FALLBACK_CNY_PER_SECOND}
    return {
        "provider": provider, "model": model, "resolution": resolution, "known": True, "unit": price.unit, "cny": price.cny,
        **({"per_video": dict(price.per_video)} if price.per_video else {}),
        "verified": price.verified, "source": price.source, "note": price.note, "unverified_margin": UNVERIFIED_MARGIN,
    }
