"""接收文本和图片路径，展示自然回复、计划及本轮图片结果。"""

import argparse
import sys

from .application import FashionVisualAgent
from .config import Settings
from .providers.llm.qwen import QwenClient
from .schemas.actions import ExecutionPlan
from .schemas.images import AgentResponse


def format_plan(plan: ExecutionPlan) -> str:
    lines = [f"任务：{plan.goal}"]
    if plan.steps:
        lines.append("计划：")
    for number, step in enumerate(plan.steps, start=1):
        lines.append(f"{number}. {step.action}")
        if step.inputs:
            lines.append("   输入：" + "；".join(step.inputs))
    if plan.questions:
        lines.extend(["", "需要你补充："])
        lines.extend(f"- {question}" for question in plan.questions)
    return "\n".join(lines)


def format_response(response: AgentResponse) -> str:
    blocks = [response.message]
    if (
        response.plan is not None
        and response.plan.status == "ready"
        and response.plan.tool_call is None
    ):
        blocks.append(format_plan(response.plan))
    if response.result is not None and response.result.images:
        lines = ["图片结果："]
        for index, image in enumerate(response.result.images, start=1):
            lines.extend([f"{index}. {image.id}", f"   {image.path}"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def main(argv: list[str] | None = None) -> int:

    # 使用 UTF-8，保证 Windows 终端和重定向文件中的中文可读。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="服饰视觉 Agent：千问规划与图像工具")
    parser.add_argument("--input", help="单次输入并退出；不传时进入多轮对话")
    parser.add_argument("--env-file", help="本地配置文件路径，默认读取当前目录的 .env")
    parser.add_argument("--output-dir", default="data/outputs", help="生成图片的保存目录")
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.env_file)

    agent = FashionVisualAgent(QwenClient(settings), output_dir=args.output_dir)

    def progress(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    def submit(text: str) -> None:
        progress("正在理解需求…")
        response = agent.run(text, on_progress=progress)
        print(format_response(response), flush=True)

    if args.input is not None:
        submit(args.input)
        return 0

    print(f"服饰视觉 Agent（{settings.model}，思考模式已开启）", file=sys.stderr)
    print("可以聊天、提出需求或继续补充。/new 开始新对话，/exit 退出。", file=sys.stderr)
    print("每轮最多执行一次图像调用；对话和图片引用仅在本次运行期间保留。", file=sys.stderr)

    # 进入多轮对话循环
    while True:
        try:
            print("\n你：", end="", file=sys.stderr, flush=True)
            text = input().strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。", file=sys.stderr)
            break

        if text.lower() in {"/exit", "/quit"}:
            break
        if text.lower() == "/new":
            agent.reset()
            print("已清空当前对话，请输入新的需求。", file=sys.stderr)
        elif text:
            submit(text)
    return 0
