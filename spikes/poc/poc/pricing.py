"""LLM 单价（元 / 百万 tokens），按 usage 估算单次调用费用（cost_basis=estimate）。

来源说明：DeepSeek 官方价格页（api-docs.deepseek.com）被当前云环境的网络策略拦截，仓库内也没有单价资料。
下表是 P0-03 冒烟时用“调用前后账户余额差 ÷ 实测 usage”校准的值（见 docs/reports/p0/P0-03.md），
偏保守取整；账户余额只精确到 0.01 元，所以按批次复核，单次费用仍是估算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Price:
    input_cache_hit: float
    input_cache_miss: float
    output: float  # 含思维链（reasoning）tokens
    source: str


# 2026-09-30 校准，见模块说明
PRICES: dict[tuple[str, str], Price] = {
    ("deepseek", "deepseek-flash"): Price(0.5, 2.0, 8.0, "2026-09-30 余额差校准（保守上界）"),
    ("deepseek", "deepseek-v4-pro"): Price(1.0, 4.0, 16.0, "2026-09-30 余额差校准（保守上界）"),
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
