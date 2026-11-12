"""管理网页的匿名会话和每个会话的图片文件。"""

import re
import shutil
import sys
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from time import monotonic
from typing import BinaryIO
from uuid import uuid4

from PIL import Image

from .application import FashionVisualAgent
from .providers.llm.qwen import QwenClient
from .schemas.images import GeneratedImage, ImageGenerationResult


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SESSION_IDLE_SECONDS = 24 * 60 * 60
_IMAGE_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
_SESSION_DIRECTORY = re.compile(r"session-[0-9a-f]{32}")


def _lock_directory(root: Path) -> BinaryIO:
    """服务启动会清理旧会话，同一目录由一个进程管理。"""
    lock = (root / ".service.lock").open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise RuntimeError("该会话目录已由另一个网页服务使用。") from None
    return lock


@dataclass
class ChatMessage:
    text: str
    sent: bool = False
    attachments: dict[str, str] = field(default_factory=dict)
    result: ImageGenerationResult | None = None


@dataclass
class WebSession:
    agent: FashionVisualAgent
    directory: Path
    messages: list[ChatMessage] = field(default_factory=list)
    draft: dict[str, str] = field(default_factory=dict)
    busy: bool = False
    last_activity: float = field(default_factory=monotonic)

    def touch(self) -> None:
        self.last_activity = monotonic()

    def save_upload(self, content: bytes) -> str:
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("请上传大小不超过 10 MiB 的图片。")

        try:
            with Image.open(BytesIO(content), formats=list(_IMAGE_EXTENSIONS)) as image:
                extension = _IMAGE_EXTENSIONS[image.format]
                image.verify()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError("请上传可读取的 PNG、JPEG 或 WebP 图片。") from exc

        image_id = f"upload-{uuid4().hex}"
        directory = self.directory / "uploads"
        directory.mkdir(exist_ok=True)
        path = directory / f"{image_id}{extension}"
        path.write_bytes(content)
        self.agent.images[image_id] = GeneratedImage(id=image_id, path=path)
        return image_id


class SessionStore:
    def __init__(self, provider: QwenClient, root: Path) -> None:
        self._provider = provider
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = _lock_directory(self._root)
        self._sessions: dict[str, WebSession] = {}

    def close(self) -> None:
        self._lock.close()

    def get(self, browser_id: str) -> WebSession | None:
        session = self._sessions.get(browser_id)
        if session is not None and (
            session.busy or monotonic() - session.last_activity < SESSION_IDLE_SECONDS
        ):
            return session
        return None

    def open(self, browser_id: str) -> WebSession:
        if (session := self.get(browser_id)) is not None:
            return session

        self._forget(browser_id)
        directory = self._root / f"session-{uuid4().hex}"
        directory.mkdir()
        agent = FashionVisualAgent(
            self._provider,
            output_dir=directory / "outputs",
            allow_local_image_paths=False,
        )
        session = WebSession(agent, directory)
        self._sessions[browser_id] = session
        return session

    def cleanup(self) -> None:
        # 空闲满 24 小时且没有任务正在处理的会话会被 _forget() 移除
        for browser_id in list(self._sessions):
            if self.get(browser_id) is None:
                self._forget(browser_id)

        active_directories = {session.directory for session in self._sessions.values()}
        for directory in self._root.iterdir():
            if (
                directory in active_directories
                or not _SESSION_DIRECTORY.fullmatch(directory.name)
                or not directory.is_dir()
                or directory.is_symlink()
                or directory.is_junction()
                or directory.resolve().parent != self._root
            ):
                continue
            # Windows 可能暂时锁定正在读取的图片，下次清理时再尝试。
            shutil.rmtree(directory, ignore_errors=True)

    def _forget(self, browser_id: str) -> None:
        if (session := self._sessions.pop(browser_id, None)) is not None:
            session.agent.reset()
            session.messages.clear()
            session.draft.clear()
