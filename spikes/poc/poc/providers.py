"""候选供应商注册表：能力类别、密钥环境变量、免费探测方式、需要放行的域名。

这是 P0 评测的候选全集，不代表选定；每个步骤（P0-03 ~ P0-10）自己决定评测哪几家。

探测规则：只使用**免费、只读**的 GET 接口（list models / account / user），绝不调用生成类接口。
无法确认存在免费校验接口的供应商，probe 为 None，doctor 只检查变量是否已配置。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# 能力类别 → 显示名；与 docs/architecture.md §7.1 的能力域对应
CAPABILITIES: dict[str, str] = {
    "llm": "LLM",
    "tts": "配音 TTS",
    "image": "图像",
    "video": "视频",
    "lipsync": "口型",
    "music_sfx": "音乐音效",
}

Env = Mapping[str, str]


def _bearer(var: str) -> Callable[[Env], dict[str, str]]:
    return lambda env: {"Authorization": f"Bearer {env[var]}"}


def _header(name: str, var: str, **extra: str) -> Callable[[Env], dict[str, str]]:
    return lambda env: {name: env[var], **extra}


@dataclass(frozen=True)
class Probe:
    """一个免费的只读 GET 接口。headers 根据环境变量生成鉴权头。"""

    url: str
    headers: Callable[[Env], dict[str, str]]
    description: str


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    capabilities: tuple[str, ...]
    env: tuple[str, ...]  # 全部必需
    domains: tuple[str, ...]
    probe: Probe | None = None
    optional_env: tuple[str, ...] = field(default=())
    notes: str = ""


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        name="anthropic",
        label="Anthropic",
        capabilities=("llm",),
        env=("ANTHROPIC_API_KEY",),
        domains=("api.anthropic.com",),
        probe=Probe(
            url="https://api.anthropic.com/v1/models",
            headers=_header("x-api-key", "ANTHROPIC_API_KEY", **{"anthropic-version": "2023-06-01"}),
            description="GET /v1/models",
        ),
    ),
    Provider(
        name="dashscope",
        label="阿里云百炼 DashScope",
        capabilities=("llm", "tts", "image", "video"),
        env=("DASHSCOPE_API_KEY",),
        domains=("dashscope.aliyuncs.com",),
        probe=Probe(
            url="https://dashscope.aliyuncs.com/compatible-mode/v1/models",
            headers=_bearer("DASHSCOPE_API_KEY"),
            description="GET /compatible-mode/v1/models",
        ),
        notes="国际站为 dashscope-intl.aliyuncs.com，密钥不通用",
    ),
    Provider(
        name="deepseek",
        label="DeepSeek",
        capabilities=("llm",),
        env=("DEEPSEEK_API_KEY",),
        domains=("api.deepseek.com",),
        probe=Probe(
            url="https://api.deepseek.com/models",
            headers=_bearer("DEEPSEEK_API_KEY"),
            description="GET /models",
        ),
    ),
    Provider(
        name="ark",
        label="火山方舟 Ark",
        capabilities=("llm", "image", "video"),
        env=("ARK_API_KEY",),
        domains=("ark.cn-beijing.volces.com",),
        notes="未确认免费校验接口，只检查是否配置",
    ),
    Provider(
        name="minimax",
        label="MiniMax",
        capabilities=("tts", "video", "music_sfx"),
        env=("MINIMAX_API_KEY",),
        optional_env=("MINIMAX_GROUP_ID",),
        domains=("api.minimaxi.com",),
        notes="未确认免费校验接口，只检查是否配置；国际站为 api.minimax.io",
    ),
    Provider(
        name="elevenlabs",
        label="ElevenLabs",
        capabilities=("tts", "music_sfx"),
        env=("ELEVENLABS_API_KEY",),
        domains=("api.elevenlabs.io",),
        probe=Probe(
            url="https://api.elevenlabs.io/v1/user",
            headers=_header("xi-api-key", "ELEVENLABS_API_KEY"),
            description="GET /v1/user",
        ),
    ),
    Provider(
        name="kling",
        label="可灵 Kling",
        capabilities=("image", "video", "lipsync"),
        env=("KLING_ACCESS_KEY", "KLING_SECRET_KEY"),
        domains=("api-beijing.klingai.com",),
        notes="需本地用 HS256 签 JWT；只检查是否配置",
    ),
    Provider(
        name="fal",
        label="fal",
        capabilities=("image", "video", "lipsync"),
        env=("FAL_KEY",),
        domains=("fal.run", "queue.fal.run"),
        notes="未确认免费校验接口，只检查是否配置",
    ),
    Provider(
        name="replicate",
        label="Replicate",
        capabilities=("image", "video", "lipsync", "music_sfx"),
        env=("REPLICATE_API_TOKEN",),
        domains=("api.replicate.com", "replicate.delivery"),
        probe=Probe(
            url="https://api.replicate.com/v1/account",
            headers=_bearer("REPLICATE_API_TOKEN"),
            description="GET /v1/account",
        ),
    ),
    Provider(
        name="gemini",
        label="Google Gemini / Veo",
        capabilities=("llm", "video"),
        env=("GEMINI_API_KEY",),
        domains=("generativelanguage.googleapis.com",),
        probe=Probe(
            url="https://generativelanguage.googleapis.com/v1beta/models",
            headers=_header("x-goog-api-key", "GEMINI_API_KEY"),
            description="GET /v1beta/models",
        ),
    ),
    Provider(
        name="runway",
        label="Runway",
        capabilities=("video",),
        env=("RUNWAYML_API_SECRET",),
        domains=("api.dev.runwayml.com",),
        probe=Probe(
            url="https://api.dev.runwayml.com/v1/organization",
            headers=lambda env: {
                "Authorization": f"Bearer {env['RUNWAYML_API_SECRET']}",
                "X-Runway-Version": "2024-11-06",
            },
            description="GET /v1/organization",
        ),
    ),
)

BY_NAME: dict[str, Provider] = {p.name: p for p in PROVIDERS}


def all_env_vars() -> list[str]:
    """注册表中出现的全部环境变量名（必需 + 可选），按注册顺序去重。"""
    names: list[str] = []
    for p in PROVIDERS:
        for var in (*p.env, *p.optional_env):
            if var not in names:
                names.append(var)
    return names


def secret_values(env: Env) -> list[str]:
    """当前环境中已配置的密钥值，只用于在输出前做脱敏，绝不写出。"""
    return [env[v] for v in all_env_vars() if env.get(v)]
