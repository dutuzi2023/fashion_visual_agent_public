"""请求千问，按指定结构返回规划或工具结果反馈。"""

import json
from urllib.request import Request, urlopen

from pydantic import BaseModel

from ...config import Settings
from ...schemas.actions import ModelResponse


class QwenClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        response_model: type[BaseModel] = ModelResponse,
    ) -> str:
        payload = {
            "model": self._settings.model,
            "messages": messages,
            "enable_thinking": True,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": response_model.model_json_schema(),
                },
            },
        }

        request = Request(
            self._settings.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=self._settings.timeout_seconds) as response:
            result = json.load(response)

        # content 是最终回答；reasoning_content 是模型内部思考，不写入对话历史。
        return result["choices"][0]["message"]["content"]
