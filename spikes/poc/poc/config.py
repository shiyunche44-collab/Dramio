"""配置加载：从 spikes/poc/.env 读取密钥，进程环境变量优先。

.env 的位置按包的位置确定，不依赖当前工作目录；可用 POC_ENV_FILE 覆盖（测试用）。
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

# spikes/poc/
PROJECT_DIR = Path(__file__).resolve().parent.parent


def env_file_path(environ: MutableMapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get("POC_ENV_FILE")
    return Path(override) if override else PROJECT_DIR / ".env"


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 格式；支持 `export ` 前缀、单双引号、`#` 注释和空行。"""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_env(path: Path | None = None, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """把 .env 中的值写入 environ（默认 os.environ），已存在的变量不覆盖。

    返回从文件中实际载入的变量名（不返回值）。文件不存在时什么也不做。
    """
    environ = os.environ if environ is None else environ
    path = env_file_path(environ) if path is None else path
    if not path.is_file():
        return []
    loaded = []
    for key, value in parse_env_file(path).items():
        if key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded


def redact(text: str, secrets: list[str]) -> str:
    """把文本中出现的密钥值替换为 ***，用于记录错误信息前的兜底。"""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text
