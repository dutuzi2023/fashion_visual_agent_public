"""图像工具返回的图片，以及 Agent 本轮的回复和结果。"""
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from .actions import ExecutionPlan


class GeneratedImage(BaseModel):
    """一张可以在后续对话中引用的图片。"""

    id: str
    path: Path


class ImageGenerationResult(BaseModel):
    """一次套图生成或穿衣调用的结果。"""

    operation: Literal["generate_model_set", "try_on"]
    images: list[GeneratedImage]


class AgentResponse(BaseModel):
    """本轮自然回复、可选计划和实际图片结果。"""

    message: str
    plan: ExecutionPlan | None = None
    result: ImageGenerationResult | None = None
