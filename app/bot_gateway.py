from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

import httpx

from .config import DestinationConfig

LOGGER = logging.getLogger(__name__)


class GatewaySession:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._last_seq: int | None = None

    def get(self) -> tuple[str | None, int | None]:
        with self._lock:
            return self._session_id, self._last_seq

    def set(self, session_id: str | None, last_seq: int | None) -> None:
        with self._lock:
            self._session_id = session_id
            self._last_seq = last_seq


async def bind_group(destination: DestinationConfig, *, timeout_seconds: float = 90.0) -> str:
    """连接 QQ 网关，等待 B 群中 @机器人发送“绑定”，返回 group_openid。"""
    secret = os.environ.get(destination.client_secret_env)
    if not secret:
        raise RuntimeError(f"环境变量 {destination.client_secret_env} 未设置")
    try:
        from qqbot_agent_sdk import EventParser, QQApiClient, QQWebSocket, WSCallbacks
    except ImportError as exc:
        raise RuntimeError("缺少 qqbot-agent-sdk，请先安装 requirements.txt") from exc

    http_client = httpx.AsyncClient(timeout=30)
    api = QQApiClient(app_id=destination.app_id, client_secret=secret)
    api.setup(http_client)
    await api.ensure_token()
    loop = asyncio.get_running_loop()
    bound: asyncio.Future[str] = loop.create_future()
    session = GatewaySession()

    def set_error(message: str) -> None:
        if not bound.done():
            bound.set_exception(RuntimeError(message))

    async def on_message(event_type: str, raw: dict[str, Any]) -> None:
        try:
            event = EventParser().parse(event_type, raw)
        except Exception:
            return
        if not event or event.chat_scope != "group" or not event.chat_id:
            return
        if "绑定" not in (event.content or "").strip():
            return
        try:
            response = await api.send_text(
                "group", event.chat_id,
                "已收到绑定请求，B 群绑定成功。",
                reply_to=event.message_id,
                markdown=False,
            )
            if not response.get("id"):
                raise RuntimeError("QQ API 未返回绑定确认消息 ID")
        except Exception as exc:
            if not bound.done():
                set_error(f"已收到绑定消息，但机器人回复失败：{exc}")
            return
        if not bound.done():
            bound.set_result(event.chat_id)

    def on_fatal_error(code: str, message: str) -> None:
        loop.call_soon_threadsafe(set_error, f"QQ 网关连接失败（{code}）：{message}")

    callbacks = WSCallbacks(
        on_message_event=on_message,
        on_connected=lambda: None,
        on_disconnected=lambda: None,
        on_fatal_error=on_fatal_error,
        get_token=api.ensure_token_sync,
        get_session=session.get,
        set_session=session.set,
        set_heartbeat_interval=lambda _: None,
        clear_token=api.clear_token,
        fail_pending=lambda _: None,
        get_gateway_url=api.get_gateway_url_sync,
    )
    websocket = QQWebSocket(callbacks=callbacks, log_tag=f"QQBot:{destination.app_id}")
    try:
        gateway_url = await asyncio.to_thread(api.get_gateway_url_sync)
        websocket.start(gateway_url, loop)
        return await asyncio.wait_for(bound, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"{timeout_seconds:.0f} 秒内未收到 B 群绑定消息，请在 B 群 @机器人 发送：绑定") from exc
    finally:
        await websocket.async_stop()
        await http_client.aclose()


async def run_gateway_forever(destination: DestinationConfig, stop_event: asyncio.Event) -> None:
    """保持机器人在线；SDK 连接异常时等待后重新建立网关连接。"""
    secret = os.environ.get(destination.client_secret_env)
    if not secret:
        LOGGER.error("机器人网关未启动：环境变量 %s 未设置", destination.client_secret_env)
        return
    try:
        from qqbot_agent_sdk import QQApiClient, QQWebSocket, WSCallbacks
    except ImportError:
        LOGGER.exception("机器人网关未启动：缺少 qqbot-agent-sdk")
        return

    loop = asyncio.get_running_loop()
    session = GatewaySession()
    while not stop_event.is_set():
        http_client = httpx.AsyncClient(timeout=30)
        api = QQApiClient(app_id=destination.app_id, client_secret=secret)
        api.setup(http_client)
        failure = asyncio.Event()

        def on_fatal_error(code: str, message: str) -> None:
            LOGGER.error("QQ 网关错误 code=%s message=%s", code, message)
            loop.call_soon_threadsafe(failure.set)

        callbacks = WSCallbacks(
            on_message_event=_ignore_message,
            on_connected=lambda: LOGGER.info("QQ 机器人网关已连接"),
            on_disconnected=lambda: LOGGER.warning("QQ 机器人网关已断开"),
            on_fatal_error=on_fatal_error,
            get_token=api.ensure_token_sync,
            get_session=session.get,
            set_session=session.set,
            set_heartbeat_interval=lambda _: None,
            clear_token=api.clear_token,
            fail_pending=lambda _: None,
            get_gateway_url=api.get_gateway_url_sync,
        )
        websocket = QQWebSocket(callbacks=callbacks, log_tag=f"QQBot:{destination.app_id}")
        try:
            await api.ensure_token()
            gateway_url = await asyncio.to_thread(api.get_gateway_url_sync)
            websocket.start(gateway_url, loop)
            stop_task = asyncio.create_task(stop_event.wait())
            failure_task = asyncio.create_task(failure.wait())
            done, pending = await asyncio.wait(
                {stop_task, failure_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if stop_task in done:
                break
            LOGGER.warning("QQ 机器人网关将于 3 秒后重连")
            await asyncio.sleep(3)
        except Exception as exc:
            LOGGER.error("QQ 机器人网关启动失败：%s", type(exc).__name__)
            if not stop_event.is_set():
                await asyncio.sleep(5)
        finally:
            await websocket.async_stop()
            await http_client.aclose()


async def _ignore_message(_event_type: str, _raw: dict[str, Any]) -> None:
    """转发服务不消费 B 群事件，避免机器人自身消息形成回路。"""
