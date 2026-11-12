"""定义模型回复、执行计划和本轮工具参数。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict


class GenerateModelSetCall(BaseModel):
    """本轮套图调用需要的参数。"""

    model_config = ConfigDict(extra="forbid")

    name: Literal["generate_model_set"]
    description: str


class TryOnCall(BaseModel):
    """图片引用可以是当前会话的结果编号，也可以是本地路径。"""

    model_config = ConfigDict(extra="forbid")

    name: Literal["try_on"]
    clothing_image: str
    model_image: str


class PlanStep(BaseModel):
    """一个执行步骤。"""

    model_config = ConfigDict(extra="forbid")

    action: str
    inputs: list[str]


class ExecutionPlan(BaseModel):
    """当前需求对应的完整计划。"""

    model_config = ConfigDict(extra="forbid")

    goal: str
    status: Literal["ready", "needs_input"]
    steps: list[PlanStep]
    questions: list[str]
    tool_call: GenerateModelSetCall | TryOnCall | None


class ModelResponse(BaseModel):
    """模型本轮的自然回复；只有处理业务任务时才附带计划。"""

    model_config = ConfigDict(extra="forbid")

    message: str
    plan: ExecutionPlan | None


class FinalResponse(BaseModel):
    """模型依据实际工具结果生成的最终答复。"""

    model_config = ConfigDict(extra="forbid")

    message: str
