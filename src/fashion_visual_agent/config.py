"""从本地 .env 和环境变量读取模型配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.8-flash"


@dataclass
class Settings:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> Settings:
        path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
        local_values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
        # 已有环境变量优先于 .env 文件。
        values = {**local_values, **os.environ}
        return cls(
            api_key=(values.get("DASHSCOPE_API_KEY") or "").strip(),
            base_url=values.get("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL).strip(),
            model=values.get("QWEN_MODEL", DEFAULT_MODEL).strip(),
            timeout_seconds=float(values.get("QWEN_TIMEOUT_SECONDS", "120")),
        )
