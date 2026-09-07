from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import shutil
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import NapcatConfig, SourceConfig
from ..environment import read_user_environment_variable
from ..models import IncomingMessage
from .napcat_protocol import image_resource, image_segments, parse_message_event


LOGGER = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class NapcatOneBotSource:
    """OneBot 11 WebSocket message source for a local NapCat instance."""

    def __init__(
        self,
        source_config: SourceConfig,
        napcat_config: NapcatConfig,
        staging_dir: Path | None = None,
    ) -> None:
        self.source_config = source_config
        self.config = napcat_config
        self.staging_dir = (staging_dir or Path("data/image-cache")).resolve()
        self._websocket: Any = None
        self._http_client: httpx.AsyncClient | None = None
        self._pending_api: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._echo_counter = 0
        self._status = "stopped"
        self._last_event_at: str | None = None
        self._last_event_type: str | None = None
        self._last_error: str | None = None
        self._reconnect_count = 0

    @property
    def status(self) -> dict[str, Any]:
        return {
            "source_backend": "napcat",
            "source_status": self._status,
            "last_event_at": self._last_event_at,
            "last_event_type": self._last_event_type,
            "reconnect_count": self._reconnect_count,
            "last_error": self._last_error,
        }

    async def close(self) -> None:
        self._closed = True
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            close = getattr(websocket, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        for future in self._pending_api.values():
            if not future.done():
                future.cancel()
        self._pending_api.clear()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._status = "stopped"

    async def run(
        self,
        output: asyncio.Queue[list[IncomingMessage]],
        stop_event: asyncio.Event,
    ) -> None:
        token = read_user_environment_variable(self.config.token_env)
        if not token:
            self._status = "error"
            self._last_error = f"当前用户环境变量 {self.config.token_env} 未设置"
            raise RuntimeError(self._last_error)
        self._closed = False
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._http_client = httpx.AsyncClient(timeout=self.config.download_timeout_seconds, follow_redirects=True)
        delay = self.config.reconnect_min_seconds
        try:
            while not stop_event.is_set() and not self._closed:
                try:
                    await self._run_connection(output, stop_event, token)
                    delay = self.config.reconnect_min_seconds
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._status = "reconnecting"
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._reconnect_count += 1
                    LOGGER.warning("NapCat OneBot 连接中断，将在 %.1f 秒后重连 error=%s", delay, type(exc).__name__)
                    if stop_event.is_set() or self._closed:
                        break
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    delay = min(delay * 2, self.config.reconnect_max_seconds)
        finally:
            await self.close()

    async def _connect(self, token: str) -> Any:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("缺少 websockets，请先安装 requirements.txt") from exc
        headers = {"Authorization": f"Bearer {token}"}
        kwargs = {
            "open_timeout": self.config.connect_timeout_seconds,
            "ping_interval": 20,
            "ping_timeout": self.config.heartbeat_timeout_seconds,
        }
        try:
            return await websockets.connect(self.config.ws_url, additional_headers=headers, **kwargs)
        except TypeError as exc:
            # websockets 12/13 named this argument extra_headers; keep the
            # adapter compatible with both supported client API generations.
            if "additional_headers" not in str(exc):
                raise
            return await websockets.connect(self.config.ws_url, extra_headers=headers, **kwargs)

    async def _run_connection(
        self,
        output: asyncio.Queue[list[IncomingMessage]],
        stop_event: asyncio.Event,
        token: str,
    ) -> None:
        websocket = await self._connect(token)
        self._websocket = websocket
        self._status = "connected"
        self._last_error = None
        LOGGER.info("NapCat OneBot 已连接 url=%s", self.config.ws_url)
        try:
            while not stop_event.is_set() and not self._closed:
                recv_task = asyncio.create_task(websocket.recv(), name="napcat-websocket-recv")
                stop_task = asyncio.create_task(stop_event.wait(), name="napcat-stop-waiter")
                try:
                    done, _pending = await asyncio.wait(
                        {recv_task, stop_task},
                        timeout=self.config.heartbeat_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop_task in done:
                        recv_task.cancel()
                        await asyncio.gather(recv_task, return_exceptions=True)
                        return
                    if not done:
                        recv_task.cancel()
                        await asyncio.gather(recv_task, return_exceptions=True)
                        raise RuntimeError("超过心跳超时时间未收到 NapCat 数据")
                    raw = recv_task.result()
                finally:
                    if not stop_task.done():
                        stop_task.cancel()
                    await asyncio.gather(stop_task, return_exceptions=True)
                if raw is None:
                    raise RuntimeError("NapCat WebSocket 已关闭")
                try:
                    event = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    LOGGER.warning("忽略无法解析的 NapCat WebSocket 数据")
                    continue
                if not isinstance(event, dict):
                    continue
                if self._resolve_api_response(event):
                    continue
                post_type = event.get("post_type")
                self._last_event_type = str(post_type or "unknown")
                if post_type != "message":
                    continue
                task = asyncio.create_task(self._handle_message(event, output), name="napcat-message-handler")
                self._handlers.add(task)
                task.add_done_callback(self._handler_done)
        finally:
            if self._websocket is websocket:
                self._websocket = None
            close = getattr(websocket, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    def _handler_done(self, task: asyncio.Task[None]) -> None:
        self._handlers.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            LOGGER.warning("NapCat 消息处理失败 error=%s", type(exc).__name__)

    async def _handle_message(
        self,
        event: dict[str, Any],
        output: asyncio.Queue[list[IncomingMessage]],
    ) -> None:
        message = parse_message_event(event, self.source_config.sessions)
        if message is None:
            return
        self._last_event_at = message.observed_at
        if image_segments(event):
            try:
                media_path = await self._stage_image(event, message.message_key)
            except Exception as exc:
                # A missing original must be handled as a failed queue item by
                # the main pipeline; never turn it into a sendable placeholder.
                LOGGER.warning("NapCat 图片下载失败 key=%s error=%s", message.message_key[:12], type(exc).__name__)
                message = IncomingMessage(
                    **{**message.__dict__, "media_path": None, "kind": "image"}
                )
            else:
                message = IncomingMessage(
                    **{**message.__dict__, "media_path": str(media_path), "kind": "image"}
                )
        await output.put([message])

    async def _stage_image(self, event: dict[str, Any], message_key: str) -> Path:
        segments = image_segments(event)
        if not segments:
            raise RuntimeError("消息中没有图片段")
        url, file_value = image_resource(segments[0])
        if not url and file_value:
            api_result = await self._call_api("get_image", {"file": file_value})
            data = api_result.get("data")
            if isinstance(data, dict):
                url = str(data.get("url") or "").strip() or None
                file_value = str(data.get("file") or data.get("path") or "").strip() or file_value
        digest = hashlib.sha256(message_key.encode("utf-8")).hexdigest()
        part = self.staging_dir / f"{digest}.part"
        if file_value:
            local_path = Path(file_value)
            if local_path.exists() and local_path.is_file():
                await asyncio.to_thread(shutil.copyfile, local_path, part)
            elif url:
                await self._download_url(url, part)
            else:
                raise RuntimeError("NapCat 未返回可读取的图片路径或 URL")
        elif url:
            await self._download_url(url, part)
        else:
            raise RuntimeError("NapCat 图片段没有 file、path 或 url")
        try:
            size = part.stat().st_size
            if size <= 0 or size > MAX_IMAGE_BYTES:
                raise RuntimeError(f"图片大小无效：{size} bytes")
            suffix = await asyncio.to_thread(self._image_suffix, part, url)
            final_path = self.staging_dir / f"{digest}{suffix}"
            part.replace(final_path)
            return final_path
        except Exception:
            part.unlink(missing_ok=True)
            raise

    async def _download_url(self, url: str, target: Path) -> None:
        if self._http_client is None:
            raise RuntimeError("图片下载器尚未启动")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError("图片 URL 协议不受支持")
        async with self._http_client.stream("GET", url) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                raise RuntimeError("图片超过 20 MB 限制")
            with target.open("wb") as handle:
                total = 0
                async for chunk in response.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise RuntimeError("图片超过 20 MB 限制")
                    handle.write(chunk)

    @staticmethod
    def _image_suffix(path: Path, url: str | None) -> str:
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
                format_name = (image.format or "").lower()
            suffix = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp", "bmp": ".bmp"}.get(format_name)
            if suffix:
                return suffix
        except Exception as exc:
            raise RuntimeError("下载内容不是有效图片") from exc
        url_suffix = Path(urlparse(url or "").path).suffix.lower()
        return url_suffix if url_suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"} else ".jpg"

    async def _call_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        websocket = self._websocket
        if websocket is None:
            raise RuntimeError("NapCat WebSocket 尚未连接")
        self._echo_counter += 1
        echo = f"qq-forwarder-{self._echo_counter}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_api[echo] = future
        try:
            async with self._send_lock:
                await websocket.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout=self.config.download_timeout_seconds)
        finally:
            self._pending_api.pop(echo, None)

    def _resolve_api_response(self, event: dict[str, Any]) -> bool:
        echo = event.get("echo")
        if echo is None:
            return False
        future = self._pending_api.get(str(echo))
        if future is None or future.done():
            return False
        if event.get("status") not in {None, "ok"} or event.get("retcode", 0) not in {0, None}:
            future.set_exception(RuntimeError(f"NapCat API {event.get('retcode', 'unknown')} 调用失败"))
        else:
            future.set_result(event)
        return True
