"""模特套图生成和 AI 穿衣接口。"""
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from ...schemas.images import GeneratedImage, ImageGenerationResult


# 两个图像工具共用的等待时间，单位为秒。
GENERATION_DELAY_SECONDS = 10.0


def generate_model_set(
    description: str,
    *,
    output_dir: str | Path = "data/outputs",
    on_start: Callable[[], None] | None = None,
) -> ImageGenerationResult:
    """根据文本描述生成四张模特图。"""
    if not description.strip():
        raise ValueError("模特套图生成需要非空的文本描述。")

    print("模特套图生成中....", flush=True)
    if on_start is not None:
        on_start()
    time.sleep(GENERATION_DELAY_SECONDS)
    model_set_result = _create_result("generate_model_set", "套图生成", 4, output_dir)
    print("套图生成已完成！", flush=True)

    return model_set_result


def try_on(
    clothing_image: str | Path,
    model_image: str | Path,
    *,
    output_dir: str | Path = "data/outputs",
    on_start: Callable[[], None] | None = None,
) -> ImageGenerationResult:
    """根据服装图和模特图生成一张穿衣结果图。"""

    for image_path in (clothing_image, model_image):
        with Image.open(image_path) as image:
            image.verify()

    print("AI穿衣生成中....", flush=True)
    if on_start is not None:
        on_start()
    time.sleep(GENERATION_DELAY_SECONDS)
    try_on_result = _create_result("try_on", "AI穿衣", 1, output_dir)
    print("穿衣生成已完成！", flush=True)

    return try_on_result


def _create_result(
    operation: Literal["generate_model_set", "try_on"],
    text: str,
    count: int,
    output_dir: str | Path,
) -> ImageGenerationResult:
    prefix = "G" if operation == "generate_model_set" else "T"
    batch_id = prefix + uuid4().hex[:12]
    batch_dir = Path(output_dir).resolve() / batch_id
    font = ImageFont.truetype("msyh.ttc", 64)
    batch_dir.mkdir(parents=True)

    images = []
    with Image.new("RGB", (1024, 1024), "white") as image:
        draw = ImageDraw.Draw(image)
        draw.text((512, 512), text, font=font, fill="black", anchor="mm")
        for number in range(1, count + 1):
            image_id = f"{batch_id}-{number:02d}"
            path = batch_dir / f"{image_id}.png"
            image.save(path, format="PNG")
            images.append(GeneratedImage(id=image_id, path=path))

    return ImageGenerationResult(operation=operation, images=images)
