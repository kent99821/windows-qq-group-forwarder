from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

import httpx

from ..config import DestinationConfig
from ..models import IncomingMessage


def format_forward_content(prefix: str, message: IncomingMessage) -> str:
    """Format text forwarded to B group, including the local observed time."""
    timestamp = _format_local_time(message.observed_at)
    sender = f"{message.sender}: " if message.sender else ""
    suffix = "（Windows 通知仅提供图片占位符，无法取得原图）" if message.kind == "toast_image_notice" else ""
    return f"{prefix} [{timestamp}] {sender}{message.content}{suffix}".strip()


def _format_local_time(value: str) -> str:
    """Convert the stored UTC timestamp to the computer's local display time."""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class OfficialQqBotSender:
    def __init__(self, config: DestinationConfig) -> None:
        self.config = config
        self.http_client: httpx.AsyncClient | None = None
        self.api: Any = None

    async def start(self) -> None:
        secret = os.environ.get(self.config.client_secret_env)
        if not secret:
            raise RuntimeError(f"环境变量 {self.config.client_secret_env} 未设置")
        try:
            from qqbot_agent_sdk import QQApiClient
        except ImportError as exc:
            raise RuntimeError("缺少 qqbot-agent-sdk，请先安装 requirements.txt") from exc
        self.http_client = httpx.AsyncClient(timeout=30)
        self.api = QQApiClient(app_id=self.config.app_id, client_secret=secret)
        self.api.setup(self.http_client)
        await self.api.ensure_token()

    async def close(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def send(self, message: IncomingMessage) -> None:
        if self.api is None:
            raise RuntimeError("QQ 机器人发送器尚未启动")
        if message.kind == "image" and message.media_path:
            await self._send_image(message)
            return
        content = format_forward_content(self.config.message_prefix, message)
        try:
            response = await self.api.send_text(
                "group", self.config.group_openid, content,
                reply_to=None, markdown=False,
            )
        except Exception as exc:
            raise RuntimeError(f"B 群机器人发送失败：{exc}") from exc
        if not isinstance(response, dict) or not response.get("id"):
            raise RuntimeError("B 群机器人 API 未返回消息 ID")

    async def _send_image(self, message: IncomingMessage) -> None:
        if self.http_client is None or self.api is None or not message.media_path:
            raise RuntimeError("QQ 图片发送器尚未完整启动")
        try:
            from qqbot_agent_sdk import MEDIA_TYPE_IMAGE, MediaInfo, MediaUploader, MessageToCreate, QQMessageType
            image_path = message.media_path
            uploader = MediaUploader(self.api, self.http_client, log_tag="WindowsQqForwarder")
            file_info = await uploader.upload(
                "group",
                self.config.group_openid,
                image_path,
                MEDIA_TYPE_IMAGE,
                file_name="qq-image.jpg",
            )
            if not file_info:
                raise RuntimeError("QQ 图片上传未返回 file_info")
            response = await self.api.post_group_message(
                self.config.group_openid,
                MessageToCreate(
                    msg_type=QQMessageType.RICH_MEDIA,
                    msg_seq=self.api.next_msg_seq(),
                    media=MediaInfo(file_info=file_info),
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"B 群图片上传或发送失败：{exc}") from exc
        if not isinstance(response, dict) or not response.get("id"):
            raise RuntimeError("B 群图片 API 未返回消息 ID")
