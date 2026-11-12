"""NiceGUI 聊天入口。图像文件通过当前会话的私有接口提供。"""

import argparse
import asyncio
import logging
from pathlib import Path
from secrets import token_urlsafe

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from nicegui import app, events, run, ui

from .config import Settings
from .providers.llm.qwen import QwenClient
from .schemas.images import GeneratedImage
from .web_sessions import MAX_UPLOAD_BYTES, ChatMessage, SessionStore, WebSession


IMAGE_ROLES = {"model_image": "模特图", "clothing_image": "服装图"}
IMAGE_START_MESSAGES = {
    "generate_model_set": "信息已齐全，正在为你生成模特套图，可能需要一点时间。",
    "try_on": "模特图和服装图已齐全，正在为你生成试穿图，可能需要一点时间。",
}
WEB_ASSETS = Path(__file__).with_name("web_assets")
logger = logging.getLogger(__name__)


def _chat_page(store: SessionStore, browser_id: str, session: WebSession) -> None:
    previewed_result: GeneratedImage | None = None

    def image_url(image_id: str) -> str:
        return f"/fva-images/{image_id}"

    def download_preview() -> None:
        if previewed_result is not None:
            ui.download.from_url(image_url(previewed_result.id), filename=previewed_result.path.name)

    with ui.dialog() as preview_dialog, ui.card().classes("fva-preview-dialog"):
        preview = ui.image().props("fit=contain").classes("fva-preview-image")
        with ui.row().classes("fva-preview-actions"):
            ui.button("关闭", on_click=preview_dialog.close).props("flat")
            download_button = ui.button(
                "下载图片", icon="download", on_click=download_preview
            ).props("unelevated").set_visibility(False)

    def show_preview(image_id: str) -> None:
        nonlocal previewed_result
        previewed_result = next(
            (image for result in session.agent.results for image in result.images if image.id == image_id),
            None,
        )
        download_button.set_visibility(previewed_result is not None)
        preview.set_source(image_url(image_id))
        preview_dialog.open()

    def can_edit() -> bool:
        if store.get(browser_id) is not session:
            ui.notify("会话已过期，正在开启新会话。")
            ui.navigate.reload()
            return False
        if session.busy:
            ui.notify("当前消息正在处理，请稍候。")
            return False
        return True

    def pick_model(image_id: str) -> None:
        if can_edit():
            session.draft["model_image"] = image_id
            sync_page()

    def remove_attachment(role: str) -> None:
        if can_edit():
            session.draft.pop(role, None)
            sync_page()

    def thumbnail(image_id: str) -> None:
        ui.image(image_url(image_id)).props(
            "fit=contain tabindex=0 role=button aria-label=预览图片"
        ).classes("fva-thumbnail").on("click", lambda: show_preview(image_id)).on(
            "keydown.enter", lambda: show_preview(image_id)
        ).tooltip("预览图片")

    def render_message(message: ChatMessage) -> None:
        side = "fva-message-sent" if message.sent else "fva-message-agent"
        with ui.row().classes(f"fva-message {side}"):
            ui.icon("person" if message.sent else "auto_awesome").classes("fva-message-icon")
            with ui.column().classes("fva-message-body"):
                ui.label("你" if message.sent else "Agent").classes("fva-message-author")
                with ui.column().classes("fva-message-bubble"):
                    if message.text:
                        ui.label(message.text).classes("fva-message-text")
                    if message.attachments:
                        with ui.row().classes("fva-message-media"):
                            for role, image_id in message.attachments.items():
                                with ui.column().classes("fva-result-tile"):
                                    thumbnail(image_id)
                                    ui.label(IMAGE_ROLES[role]).classes("fva-message-author")
                    if message.result is not None:
                        is_model_set = message.result.operation == "generate_model_set"
                        with ui.row().classes("fva-message-media"):
                            for image in message.result.images:
                                with ui.column().classes("fva-result-tile"):
                                    thumbnail(image.id)
                                    if is_model_set:
                                        ui.button(
                                            "选作模特",
                                            on_click=lambda image_id=image.id: pick_model(image_id),
                                        ).props("dense flat no-caps").classes("fva-select-model")

    def show_image_start(operation: str) -> None:
        session.messages.append(ChatMessage(IMAGE_START_MESSAGES[operation]))
        sync_page()

    async def submit() -> None:
        text = (message_input.value or "").strip()
        if not can_edit():
            return
        if session.draft != shown_draft:
            sync_page()
            ui.notify("附件已更新，请确认缩略图后再发送。")
            return
        if not text and not session.draft:
            return

        attachments = dict(session.draft)
        session.draft.clear()
        session.messages.append(ChatMessage(text, sent=True, attachments=attachments))
        session.touch()
        session.busy = True
        message_input.set_value("")
        sync_page()

        loop = asyncio.get_running_loop()

        def on_image_start(operation: str) -> None:
            loop.call_soon_threadsafe(show_image_start, operation)

        result_count = len(session.agent.results)
        try:
            response = await run.io_bound(
                session.agent.run, text, attachments=attachments, on_image_start=on_image_start
            )
            session.messages.append(ChatMessage(response.message, result=response.result))
        except Exception as exc:
            logger.warning("网页 Agent 处理失败（%s）", type(exc).__name__)
            if len(session.agent.results) > result_count:
                session.messages.append(
                    ChatMessage(
                        "图片已返回，但暂时无法获取 Agent 的结果说明。",
                        result=session.agent.results[-1],
                    )
                )
            else:
                session.messages.append(ChatMessage("处理失败，暂时无法获取 Agent 回复。"))
        finally:
            session.busy = False
            sync_page()

    def make_uploader(role: str) -> ui.upload:
        async def save(event: events.UploadEventArguments) -> None:
            try:
                if not can_edit():
                    return
                if event.file.size() > MAX_UPLOAD_BYTES:
                    raise ValueError("单张图片最多 10 MiB。")
                content = await event.file.read()
                if can_edit():
                    session.draft[role] = session.save_upload(content)
                    session.touch()
                    sync_page()
            except ValueError as exc:
                ui.notify(str(exc), type="negative")
            except OSError:
                ui.notify("图片保存失败，请重新上传。", type="negative")

        uploader = ui.upload(
            label=f"上传{IMAGE_ROLES[role]}",
            auto_upload=True,
            max_file_size=MAX_UPLOAD_BYTES,
            on_upload=save,
            on_rejected=lambda: ui.notify("请选择 10 MiB 以内的 PNG、JPEG 或 WebP 图片。"),
        ).props('flat no-thumbnails accept="image/png,image/jpeg,image/webp"').classes("fva-uploader")
        uploader.add_slot(
            "header",
            f"""
            <q-btn class="fva-upload-button" type="button" no-caps unelevated
                icon="add_photo_alternate" label="选择图片" aria-label="选择{IMAGE_ROLES[role]}"
                :disable="!props.canAddFiles" :loading="props.isUploading"
                @click="props.pickFiles">
                <q-uploader-add-trigger></q-uploader-add-trigger>
            </q-btn>
            """,
        )
        uploader.add_slot("list", "<span hidden></span>")
        uploader.on("finish", lambda: uploader.reset(), args=[])
        uploader.on("failed", lambda: ui.notify("上传失败，请重试。", type="negative"), args=[])
        return uploader

    material_previews: dict[str, ui.row] = {}
    uploaders: list[ui.upload] = []
    with ui.column().classes("fva-app"):
        with ui.element("header").classes("fva-masthead"):
            with ui.row().classes("fva-brand"):
                ui.icon("checkroom").classes("fva-brand-icon")
                ui.label("服饰视觉 Agent").classes("fva-brand-name")

        with ui.element("main").classes("fva-workspace"):
            with ui.element("section").classes("fva-chat-panel"):
                with ui.row().classes("fva-panel-heading"):
                    ui.label("创作对话").classes("fva-panel-title")
                with ui.scroll_area().classes("fva-scroll") as scroll:
                    with ui.column().classes("fva-transcript") as transcript:
                        with ui.element("section").classes("fva-hero") as hero:
                            with ui.column().classes("fva-hero-copy"):
                                ui.label("模特套图\n与 AI 穿衣").classes("fva-hero-title").props(
                                    "role=heading aria-level=1"
                                )
                                ui.label(
                                    "描述你想要的套图，或上传模特图和服装图开始试穿。"
                                ).classes("fva-hero-detail")
                            ui.image("/fva-assets/studio-portrait.png").props(
                                'fit=cover alt="米白西装造型示例" no-spinner'
                            ).classes("fva-hero-image")
                with ui.element("footer").classes("fva-composer"):
                    with ui.row().classes("fva-working").props("role=status") as status:
                        ui.spinner("dots", size="16px")
                        ui.label("正在处理…")
                    with ui.element("div").classes("fva-composer-box"):
                        composer_previews = ui.row().classes("fva-composer-attachments").props(
                            'role=group aria-label="待发送图片"'
                        ).set_visibility(False)
                        with ui.row().classes("fva-composer-row"):
                            message_input = ui.input(placeholder="描述你的造型想法…").props(
                                "borderless aria-label=消息"
                            ).classes("fva-input")
                            message_input.on(
                                "keydown.enter",
                                submit,
                                js_handler="event => { if (!event.isComposing && event.keyCode !== 229) emit(); }",
                            )
                            send_button = ui.button(
                                "发送", icon="arrow_upward", on_click=submit
                            ).props("unelevated no-caps").classes("fva-send")

            with ui.element("aside").classes("fva-assets-panel"):
                with ui.column().classes("fva-assets-heading"):
                    ui.label("试穿素材").classes("fva-panel-title")
                    ui.label("也可以从套图结果中选用模特图。").classes("fva-assets-note")
                with ui.column().classes("fva-materials"):
                    for role, label in IMAGE_ROLES.items():
                        with ui.column().classes("fva-material"):
                            with ui.row().classes("fva-material-heading"):
                                ui.icon("person_outline" if role == "model_image" else "checkroom").classes(
                                    "fva-material-icon"
                                )
                                ui.label(label).classes("fva-material-title")
                            material_previews[role] = ui.row().classes(
                                "fva-material-preview"
                            ).set_visibility(False)
                            uploaders.append(make_uploader(role))
                ui.label("PNG、JPEG 或 WebP · 单张最多 10 MiB").classes("fva-upload-note")

    shown_messages = 0
    shown_draft: dict[str, str] = {}
    shown_busy: bool | None = None
    expired = False

    def sync_page() -> None:
        nonlocal shown_messages, shown_draft, shown_busy, expired
        if transcript.client.is_deleted:
            return
        if store.get(browser_id) is not session:
            if not expired:
                expired = True
                ui.navigate.reload()
            return
        if len(session.messages) != shown_messages:
            hero.set_visibility(False)
            with transcript:
                for message in session.messages[shown_messages:]:
                    render_message(message)
            shown_messages = len(session.messages)
            scroll.scroll_to(percent=1)
        if session.draft != shown_draft:
            composer_previews.clear()
            composer_previews.set_visibility(bool(session.draft))
            with composer_previews:
                for role, image_id in session.draft.items():
                    label = IMAGE_ROLES[role]
                    with ui.row().classes("fva-composer-attachment"):
                        thumbnail(image_id)
                        ui.label(label)
                        ui.button(
                            icon="close",
                            on_click=lambda role=role: remove_attachment(role),
                        ).props(f'dense flat round aria-label="移除{label}"').tooltip(f"移除{label}")

            for role, preview_slot in material_previews.items():
                image_id = session.draft.get(role)
                if image_id == shown_draft.get(role):
                    continue
                preview_slot.clear()
                preview_slot.set_visibility(image_id is not None)
                if image_id is not None:
                    with preview_slot:
                        thumbnail(image_id)
                        ui.button(
                            "移除",
                            on_click=lambda role=role: remove_attachment(role),
                        ).props("dense flat").classes("fva-material-remove")
            shown_draft = dict(session.draft)
        if session.busy != shown_busy:
            shown_busy = session.busy
            status.set_visibility(session.busy)
            for control in (message_input, send_button, *uploaders):
                control.set_enabled(not session.busy)

    sync_page()
    ui.timer(1, sync_page)


def _configure_routes(store: SessionStore) -> None:
    app.add_static_files("/fva-assets", WEB_ASSETS)
    ui.add_css(WEB_ASSETS / "studio.css", shared=True)
    app.colors(primary="#563048")

    @ui.page("/")
    async def home(request: Request) -> None:
        browser_id = request.session["id"]
        _chat_page(store, browser_id, store.open(browser_id))

    @app.get("/fva-images/{image_id}")
    async def session_image(image_id: str, request: Request) -> FileResponse:
        session = store.get(request.session.get("id", ""))
        image = session.agent.images.get(image_id) if session is not None else None
        if image is None or not image.path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            image.path,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="服饰视觉 Agent 的 NiceGUI 网页")
    parser.add_argument("--port", type=int, default=8080, help="本机监听端口，默认 8080")
    parser.add_argument("--on-air", action="store_true", help="开启 NiceGUI On Air 临时公网分享")
    parser.add_argument("--env-file", help="本地配置文件路径，默认读取当前目录的 .env")
    args = parser.parse_args(argv)

    provider = QwenClient(Settings.from_env(args.env_file))
    store = SessionStore(provider, Path("data/web_sessions"))
    try:
        _configure_routes(store) # 路由配置
        app.on_startup(store.cleanup) #注册一个启动回调，网页服务启动时，NiceGUI调用一次store.cleanup()
        app.timer(60, store.cleanup) #注册一个定时器，让 NiceGUI 每隔 60 秒调用 store.cleanup()
        ui.run(
            host="127.0.0.1",
            port=args.port,
            title="服饰视觉 Agent",
            show=False,
            reload=False,
            on_air=args.on_air,
            storage_secret=token_urlsafe(32),
            session_middleware_kwargs={
                "session_cookie": f"fva_session_{args.port}",
                "https_only": args.on_air,
                "max_age": None,
            },
        )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
