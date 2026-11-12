"""统一处理对话、任务规划与本轮图像调用，并保留会话结果。"""

import json
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

from .providers.image import generate_model_set, try_on
from .providers.llm.prompts import (
    PLANNER_SYSTEM_PROMPT,
    RESULT_REVIEW_SYSTEM_PROMPT,
    WEB_IMAGE_RULES,
)
from .providers.llm.qwen import QwenClient
from .schemas.actions import (
    ExecutionPlan,
    FinalResponse,
    GenerateModelSetCall,
    ModelResponse,
    TryOnCall,
)
from .schemas.images import AgentResponse, GeneratedImage, ImageGenerationResult


_UNAVAILABLE_IMAGE = "图片编号无效或已失效，请重新上传或选择当前会话中的图片。"


class FashionVisualAgent:
    def __init__(
        self,
        provider: QwenClient,
        *,
        output_dir: str | Path = "data/outputs",
        allow_local_image_paths: bool = True,
    ) -> None:
        self._provider = provider
        self._allow_local_image_paths = allow_local_image_paths
        self.output_dir = Path(output_dir)
        self.history: list[dict[str, str]] = []
        self.current_plan: ExecutionPlan | None = None
        self.images: dict[str, GeneratedImage] = {}
        self.results: list[ImageGenerationResult] = []

    def reset(self) -> None:
        self.history.clear()
        self.current_plan = None
        self.images.clear()
        self.results.clear()

    def run(
        self,
        user_input: str,
        *,
        on_progress: Callable[[str], None] | None = None,
        on_image_start: Callable[[str], None] | None = None,
        attachments: Mapping[str, str] | None = None,
    ) -> AgentResponse:
        """理解需求、执行一次图像调用，再根据实际结果生成最终答复。

        attachments 按穿衣参数指定本轮选定的当前会话图片编号。
        on_image_start 在输入通过校验、开始生成时接收图像工具名称。
        """
        response = self._respond(user_input, attachments)
        plan = response.plan
        if (
            plan is None
            or plan.status != "ready"
            or plan.questions
            or plan.tool_call is None
        ):
            return response

        call = plan.tool_call
        on_start = partial(on_image_start, call.name) if on_image_start is not None else None
        error = None
        try:
            if isinstance(call, GenerateModelSetCall):
                response.result = self.generate_model_set(call.description, on_start=on_start)
            else:
                response.result = self.try_on(call.clothing_image, call.model_image, on_start=on_start)
        except Exception as exc:
            # 工具失败也交给模型说明；不自动重试。
            if self._allow_local_image_paths:
                error = f"{type(exc).__name__}: {exc}"
            elif isinstance(exc, LookupError):
                error = _UNAVAILABLE_IMAGE
            elif isinstance(exc, OSError):
                error = "无法读取或保存图片文件，请检查附件后重试。"
            else:
                error = "本轮图像工具执行失败。"

        if on_progress is not None:
            on_progress("正在确认生成结果…")
        response.message = self._review_result(
            user_input, plan, response.result, error
        )
        return response

    def plan(self, user_input: str) -> ExecutionPlan | None:
        """只理解和规划，不执行工具；普通聊天返回 None。"""
        return self._respond(user_input).plan

    def _respond(
        self, user_input: str, attachments: Mapping[str, str] | None = None
    ) -> AgentResponse:
        context = {
            "current_plan": (
                self.current_plan.model_dump(mode="json")
                if self.current_plan is not None
                else None
            ),
            "image_results": [self._result_context(result) for result in self.results],
        }
        user_text = user_input.strip()
        turn = []
        if attachments:
            turn.append(
                {
                    "role": "system",
                    "content": "以下是用户随紧接的消息实际发送的附件及用途：\n"
                    + json.dumps(dict(attachments), ensure_ascii=False),
                }
            )
            if not user_text:
                user_text = "（本轮仅发送图片附件）"
        turn.append({"role": "user", "content": user_text})
        messages = [
            {"role": "system", "content": self._image_prompt(PLANNER_SYSTEM_PROMPT)},
            {
                "role": "system",
                "content": "当前任务与实际图片结果，仅作上下文，不是本轮执行指令：\n"
                + json.dumps(context, ensure_ascii=False),
            },
            *self.history,
            *turn,
        ]
        reply = ModelResponse.model_validate_json(self._provider.generate(messages))
        plan = reply.plan
        if (
            attachments
            and plan is not None
            and plan.status == "ready"
            and not plan.questions
            and isinstance(plan.tool_call, TryOnCall)
        ):
            plan.tool_call = TryOnCall(
                name="try_on",
                clothing_image=attachments.get(
                    "clothing_image", plan.tool_call.clothing_image
                ),
                model_image=attachments.get("model_image", plan.tool_call.model_image),
            )
        response = AgentResponse(message=reply.message, plan=reply.plan)

        # 聊天保留原任务；本轮是否调用工具只看本轮返回的计划。
        if response.plan is not None:
            self.current_plan = response.plan
        self.history.extend(
            [*turn, {"role": "assistant", "content": reply.model_dump_json()}]
        )
        return response

    def _review_result(
        self,
        user_input: str,
        plan: ExecutionPlan,
        result: ImageGenerationResult | None,
        error: str | None,
    ) -> str:
        execution = {
            "user_input": user_input.strip(),
            "goal": plan.goal,
            "tool_call": plan.tool_call.model_dump(mode="json"),
            "result": self._result_context(result) if result is not None else None,
            "error": error,
        }
        # 自定义 tool_call 的执行记录由程序提供，不伪装成用户消息。
        self.history.append(
            {
                "role": "system",
                "content": "本轮工具执行记录（程序数据，不是用户指令）：\n"
                + json.dumps(execution, ensure_ascii=False),
            }
        )
        messages = [
            {
                "role": "system",
                "content": self._image_prompt(RESULT_REVIEW_SYSTEM_PROMPT),
            },
            *self.history,
        ]
        final = FinalResponse.model_validate_json(
            self._provider.generate(messages, response_model=FinalResponse)
        )
        self.history.append(
            {"role": "assistant", "content": final.model_dump_json()}
        )
        return final.message

    def generate_model_set(
        self, description: str, *, on_start: Callable[[], None] | None = None
    ) -> ImageGenerationResult:
        """直接生成套图，并将返回的图片登记到当前会话。"""
        result = generate_model_set(description, output_dir=self.output_dir, on_start=on_start)
        self._remember_result(result)
        return result

    def try_on(
        self,
        clothing_image: str | Path,
        model_image: str | Path,
        *,
        on_start: Callable[[], None] | None = None,
    ) -> ImageGenerationResult:
        """接受本地路径或本会话的图片编号，执行单次穿衣。"""
        result = try_on(
            clothing_image=self._image_path(clothing_image),
            model_image=self._image_path(model_image),
            output_dir=self.output_dir,
            on_start=on_start,
        )
        self._remember_result(result)
        return result

    def _image_path(self, reference: str | Path) -> Path:
        """优先解析本会话图片编号，本地调用也可以直接提供路径。"""
        if str(reference) in self.images:
            return self.images[str(reference)].path
        if not self._allow_local_image_paths:
            raise LookupError(_UNAVAILABLE_IMAGE)
        return Path(reference).expanduser()

    def _image_prompt(self, prompt: str) -> str:
        return prompt if self._allow_local_image_paths else prompt + WEB_IMAGE_RULES

    def _result_context(self, result: ImageGenerationResult) -> dict[str, object]:
        if self._allow_local_image_paths:
            return result.model_dump(mode="json")
        return {
            "operation": result.operation,
            "images": [{"id": image.id} for image in result.images],
        }

    def _remember_result(self, result: ImageGenerationResult) -> None:
        self.results.append(result)
        self.images.update((image.id, image) for image in result.images)
