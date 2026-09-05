from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse

from .bot_gateway import bind_group
from .config import AppConfig, load_config, save_dry_run, save_group_openid, save_listener_names
from .destination.qq_bot import OfficialQqBotSender
from .models import IncomingMessage
from .preflight import run_preflight
from .source.qq_history_reader import HistoryRecord, QqHistoryReader
from .source.windows_notification import WindowsNotificationReader
from .source.qq_image_cache import QqImageCache
from .single_instance import SingleInstanceError, SingleInstanceLock
from .state_store import StateStore


class ForwarderController:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.started_at: float | None = None
        self.last_exit_code: int | None = None
        self.last_action_error: str | None = None
        self.active_dry_run: bool | None = None
        self.history_previews: dict[str, dict[str, HistoryRecord]] = {}

    def config(self) -> AppConfig:
        return load_config(self.config_path)

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _reap(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            self.last_exit_code = self.process.returncode
            self.process = None
            self.active_dry_run = None

    def _forwarder_lock_path(self) -> Path:
        try:
            return self.config().runtime.database_path.parent / "forwarder.lock"
        except Exception:
            return self.config_path.parent / "data" / "forwarder.lock"

    def _forwarder_lock_is_held(self) -> bool:
        """Detect a forwarder started by another control console or terminal."""
        probe = SingleInstanceLock(self._forwarder_lock_path(), "转发服务")
        try:
            probe.acquire()
        except SingleInstanceError:
            return True
        probe.release()
        return False

    def status(self) -> dict[str, Any]:
        with self.lock:
            self._reap()
            config_exists = self.config_path.exists()
            summary = {"pending": 0, "sent": 0, "failed": 0, "discarded": 0}
            listener_names: list[str] = []
            client_secret_configured = False
            client_secret_env: str | None = None
            config_error = None
            if config_exists:
                try:
                    config = self.config()
                    listener_names = list(config.source.listener_names)
                    client_secret_env = config.destination.client_secret_env
                    client_secret_configured = bool(os.environ.get(config.destination.client_secret_env))
                    store = StateStore(config.runtime.database_path)
                    try:
                        summary = store.summary()
                    finally:
                        store.close()
                except Exception as exc:
                    config_error = str(exc)
            own_running = self._alive()
            external_running = not own_running and self._forwarder_lock_is_held()
            return {
                "running": own_running or external_running,
                "pid": self.process.pid if self._alive() and self.process else None,
                "external_instance": external_running,
                "started_at": self.started_at,
                "last_exit_code": self.last_exit_code,
                "last_action_error": self.last_action_error,
                "config_path": str(self.config_path),
                "config_exists": config_exists,
                "config_error": config_error,
                "listener_names": listener_names,
                # 保留旧字段，便于旧版页面平滑升级。
                "listener_groups": listener_names,
                "client_secret_configured": client_secret_configured,
                "client_secret_env": client_secret_env,
                "dry_run": config.runtime.dry_run if config_exists and config_error is None else None,
                "active_dry_run": self.active_dry_run if own_running else None,
                "restart_required": (
                    own_running
                    and self.active_dry_run is not None
                    and config_error is None
                    and self.active_dry_run != config.runtime.dry_run
                ),
                "messages": summary,
            }

    def start(self, dry_run: bool | None = None) -> dict[str, Any]:
        with self.lock:
            self._reap()
            self.last_action_error = None
            if self._alive():
                return self.status()
            if self._forwarder_lock_is_held():
                raise RuntimeError("检测到已有转发服务正在运行，请先关闭原转发窗口或控制台")
            if dry_run is not None:
                if not isinstance(dry_run, bool):
                    raise ValueError("dry_run 必须是布尔值")
                save_dry_run(self.config_path, dry_run)
            config = self.config()
            effective_dry_run = config.runtime.dry_run if dry_run is None else dry_run
            if not effective_dry_run and not os.environ.get(config.destination.client_secret_env):
                raise RuntimeError(
                    f"当前 Web 控制面进程未读取环境变量 {config.destination.client_secret_env}；"
                    "请重新启动 Web 控制面后再运行检查"
                )
            check = run_preflight(self.config_path)
            if not check["ready"]:
                details = "；".join(item["detail"] for item in check["missing"])
                raise RuntimeError(f"运行前检查未通过：{details}")
            command = [sys.executable, "-m", "app.main", "run", "--config", str(self.config_path)]
            if effective_dry_run:
                command.append("--dry-run")
            log_path = config.runtime.log_path
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("a", encoding="utf-8")
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=str(self.config_path.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            except Exception:
                log_handle.close()
                raise
            # The child owns the file descriptor after spawning on Windows.
            log_handle.close()
            self.started_at = time.time()
            self.active_dry_run = effective_dry_run
            # Surface immediate startup failures instead of reporting a false success.
            for _ in range(10):
                if self.process.poll() is not None:
                    exit_code = self.process.returncode
                    self._reap()
                    raise RuntimeError(f"转发服务启动失败，退出码：{exit_code}")
                time.sleep(0.05)
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            self._reap()
            if not self._alive() or self.process is None:
                return self.status()
            process = self.process
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            else:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            self.last_exit_code = process.returncode
            self.process = None
            self.started_at = None
            self.active_dry_run = None
            return self.status()

    def restart(self, dry_run: bool | None = None) -> dict[str, Any]:
        self.stop()
        return self.start(dry_run=dry_run)

    def set_dry_run(self, dry_run: object) -> dict[str, Any]:
        with self.lock:
            if not isinstance(dry_run, bool):
                raise ValueError("dry_run 必须是布尔值")
            if self._alive() or self._forwarder_lock_is_held():
                raise RuntimeError("请先停止转发服务，再修改运行模式")
            save_dry_run(self.config_path, dry_run)
            result = self.status()
            result["restart_required"] = False
            return result

    def _require_forwarder_stopped(self, action: str = "修改监听群列表") -> None:
        if self._alive() or self._forwarder_lock_is_held():
            raise RuntimeError(f"请先停止转发服务，再{action}")

    def add_listener_name(self, listener_name: object) -> dict[str, Any]:
        with self.lock:
            self._require_forwarder_stopped()
            if not isinstance(listener_name, str) or not listener_name.strip():
                raise ValueError("监听会话名称不能为空")
            config = self.config()
            candidate = listener_name.strip()
            if any(candidate.casefold() == current.casefold() for current in config.source.listener_names):
                raise ValueError(f"监听会话已存在：{candidate}")
            names = save_listener_names(self.config_path, [*config.source.listener_names, candidate])
            return {"listener_names": list(names), "listener_groups": list(names)}

    def remove_listener_name(self, listener_name: object) -> dict[str, Any]:
        with self.lock:
            self._require_forwarder_stopped()
            if not isinstance(listener_name, str) or not listener_name.strip():
                raise ValueError("监听会话名称不能为空")
            config = self.config()
            candidate = listener_name.strip()
            names = [current for current in config.source.listener_names if current.casefold() != candidate.casefold()]
            if len(names) == len(config.source.listener_names):
                raise ValueError(f"未找到监听会话：{candidate}")
            saved = save_listener_names(self.config_path, names)
            return {"listener_names": list(saved), "listener_groups": list(saved)}

    def add_listener_group(self, group_name: object) -> dict[str, Any]:
        """Backward-compatible alias for the old group-specific API."""
        return self.add_listener_name(group_name)

    def remove_listener_group(self, group_name: object) -> dict[str, Any]:
        """Backward-compatible alias for the old group-specific API."""
        return self.remove_listener_name(group_name)

    def bind_destination_group(self) -> dict[str, Any]:
        with self.lock:
            if self._alive():
                raise RuntimeError("请先停止转发服务，再执行 B 群绑定")
        config = self.config()
        group_openid = asyncio.run(bind_group(config.destination))
        save_group_openid(self.config_path, group_openid)
        return {"group_bound": True, "group_openid_preview": f"{group_openid[:6]}...{group_openid[-4:]}"}

    def inspect_window(self) -> dict[str, Any]:
        config = self.config()
        reader = WindowsNotificationReader(config.source)
        return reader.inspect()

    def inspect_image_cache(self) -> dict[str, Any]:
        config = self.config()
        return QqImageCache(config.source).inspect()

    def preflight(self) -> dict[str, Any]:
        return run_preflight(self.config_path)

    async def _send_test_message(self, config: AppConfig) -> None:
        sender = OfficialQqBotSender(config.destination)
        try:
            await sender.start()
            await sender.send(IncomingMessage.create(
                f"manual-test:{time.time_ns()}",
                "主动消息测试",
                "QQ 主动消息测试成功。后续监听到的新消息将由本机自动转发。",
            ))
        finally:
            await sender.close()

    def send_test_message(self) -> dict[str, Any]:
        config = self.config()
        if _looks_like_unconfigured(config.destination.app_id):
            raise RuntimeError("尚未填写有效的机器人 AppID")
        if _looks_like_unconfigured(config.destination.group_openid):
            raise RuntimeError("尚未绑定 B 群，请先完成 group_openid 绑定")
        if not os.environ.get(config.destination.client_secret_env):
            raise RuntimeError(f"未读取机器人密钥环境变量 {config.destination.client_secret_env}")
        asyncio.run(self._send_test_message(config))
        value = config.destination.group_openid
        return {
            "message": "主动测试消息已发送到 B 群",
            "group_openid_preview": f"{value[:6]}…{value[-4:]}",
        }

    def failed_messages(self) -> dict[str, Any]:
        config = self.config()
        store = StateStore(config.runtime.database_path)
        try:
            items = [
                {
                    "message_key": str(row["message_key"]),
                    "source_group": str(row["source_group"]),
                    "sender": str(row["sender"]) if row["sender"] is not None else None,
                    "kind": str(row["kind"]),
                    "content": str(row["content"]),
                    "observed_at": str(row["observed_at"]),
                    "attempts": int(row["attempts"]),
                    "last_error": str(row["last_error"] or "未知错误"),
                }
                for row in store.failed()
            ]
        finally:
            store.close()
        return {"items": items, "count": len(items)}

    def retry_failed_messages(self, message_keys: object) -> dict[str, Any]:
        self._require_forwarder_stopped("重试失败消息")
        if message_keys is not None and (
            not isinstance(message_keys, list) or not all(isinstance(key, str) for key in message_keys)
        ):
            raise ValueError("message_keys 必须是字符串数组或 null")
        config = self.config()
        store = StateStore(config.runtime.database_path)
        try:
            count = store.retry_failed(message_keys)
        finally:
            store.close()
        return {"retried": count, "message": f"已将 {count} 条失败消息放回待发送队列"}

    @staticmethod
    def _history_item_id(record: HistoryRecord, index: int) -> str:
        material = "\0".join((
            record.source_group,
            record.sender or "",
            record.display_time,
            record.kind,
            record.content,
            str(record.occurrence),
            str(index),
        ))
        return hashlib.sha256(f"manual-history-preview\0{material}".encode("utf-8")).hexdigest()

    def preview_history(self, listener_name: object) -> dict[str, Any]:
        self._require_forwarder_stopped("读取 QQ 历史消息")
        if not isinstance(listener_name, str) or not listener_name.strip():
            raise ValueError("请选择要补发的 QQ 群或联系人")
        config = self.config()
        selected = next(
            (name for name in config.source.listener_names if name.casefold() == listener_name.strip().casefold()),
            None,
        )
        if selected is None:
            raise ValueError(f"未配置监听会话：{listener_name.strip()}")
        records = QqHistoryReader(config.source).read_visible(selected, settle_seconds=0.6)
        if records is None:
            raise RuntimeError("未能读取 QQ 聊天窗口，请确认 QQ 已登录且该会话可以打开")
        preview: dict[str, HistoryRecord] = {}
        items: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            item_id = self._history_item_id(record, index)
            preview[item_id] = record
            items.append({
                "message_id": item_id,
                "source_group": record.source_group,
                "sender": record.sender,
                "content": record.content,
                "display_time": record.display_time,
                "kind": record.kind,
            })
        self.history_previews[selected] = preview
        return {"listener_name": selected, "items": items, "count": len(items)}

    def replay_history(self, listener_name: object, message_ids: object) -> dict[str, Any]:
        self._require_forwarder_stopped("补发 QQ 历史消息")
        if not isinstance(listener_name, str) or not listener_name.strip():
            raise ValueError("请选择要补发的 QQ 群或联系人")
        if not isinstance(message_ids, list) or not message_ids or not all(isinstance(item, str) for item in message_ids):
            raise ValueError("请至少选择一条历史消息")
        config = self.config()
        selected = next(
            (name for name in config.source.listener_names if name.casefold() == listener_name.strip().casefold()),
            None,
        )
        preview = self.history_previews.get(selected or "")
        if selected is None or preview is None:
            raise RuntimeError("历史消息预览已失效，请重新点击查看历史消息")
        unknown = [item_id for item_id in message_ids if item_id not in preview]
        if unknown:
            raise RuntimeError("历史消息预览已变化，请重新读取后再补发")
        store = StateStore(config.runtime.database_path)
        queued = 0
        skipped = 0
        try:
            for item_id in dict.fromkeys(message_ids):
                record = preview[item_id]
                message = IncomingMessage.create(
                    f"manual-history:{item_id}",
                    record.source_group,
                    record.content,
                    sender=record.sender,
                    kind=record.kind,
                )
                if store.enqueue(message):
                    queued += 1
                else:
                    skipped += 1
        finally:
            store.close()
        return {
            "queued": queued,
            "skipped": skipped,
            "message": f"已加入待发送 {queued} 条，跳过重复 {skipped} 条；启动转发服务后发送",
        }


def _looks_like_unconfigured(value: str) -> bool:
    normalized = value.strip().casefold()
    return not normalized or "替换" in normalized or "group_openid" in normalized


class ControlHandler(BaseHTTPRequestHandler):
    controller: ForwarderController
    web_root: Path

    def log_message(self, format: str, *args: object) -> None:
        logging.getLogger("qq_forwarder.web").info(format, *args)

    def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:
            raise ValueError("请求体过大")
        if not length:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _error(self, exc: Exception, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"ok": False, "error": str(exc)}, status)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json({"ok": True, **self.controller.status()})
            return
        if path == "/api/log":
            try:
                config = self.controller.config()
                if not config.runtime.log_path.exists():
                    self._json({"ok": True, "lines": []})
                    return
                lines = config.runtime.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
                self._json({"ok": True, "lines": lines})
            except Exception as exc:
                self._error(exc)
            return
        if path == "/api/replay/failed":
            try:
                self._json({"ok": True, **self.controller.failed_messages()})
            except Exception as exc:
                self._error(exc)
            return
        if path in {"/", "/index.html"}:
            self._serve_file("index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._serve_file("app.js", "text/javascript; charset=utf-8")
            return
        if path == "/styles.css":
            self._serve_file("styles.css", "text/css; charset=utf-8")
            return
        self._json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _serve_file(self, name: str, content_type: str) -> None:
        try:
            data = (self.web_root / name).read_bytes()
        except OSError as exc:
            self._error(exc, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/actions/start":
                result = self.controller.start(body.get("dry_run"))
            elif path == "/api/actions/stop":
                result = self.controller.stop()
            elif path == "/api/actions/restart":
                result = self.controller.restart(body.get("dry_run"))
            elif path == "/api/actions/dry-run":
                result = self.controller.set_dry_run(body.get("dry_run"))
            elif path == "/api/actions/inspect-window":
                result = self.controller.inspect_window()
            elif path == "/api/actions/inspect-image-cache":
                result = self.controller.inspect_image_cache()
            elif path == "/api/actions/preflight":
                result = self.controller.preflight()
            elif path == "/api/actions/test-message":
                result = self.controller.send_test_message()
            elif path == "/api/actions/retry-failed":
                result = self.controller.retry_failed_messages(body.get("message_keys"))
            elif path == "/api/actions/history-preview":
                result = self.controller.preview_history(body.get("listener_name"))
            elif path == "/api/actions/replay-history":
                result = self.controller.replay_history(
                    body.get("listener_name"),
                    body.get("message_ids"),
                )
            elif path in {"/api/actions/listener-names", "/api/actions/listener-groups"}:
                action = body.get("action")
                if action == "add":
                    result = self.controller.add_listener_name(body.get("listener_name", body.get("group_name")))
                elif action == "remove":
                    result = self.controller.remove_listener_name(body.get("listener_name", body.get("group_name")))
                else:
                    raise ValueError("action 必须是 add 或 remove")
            elif path == "/api/actions/bind-group":
                result = self.controller.bind_destination_group()
            else:
                self._json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json({"ok": True, **result} if isinstance(result, dict) else {"ok": True, "result": result})
        except Exception as exc:
            logging.getLogger("qq_forwarder.web").exception("控制面操作失败")
            self._error(exc)


def create_server(controller: ForwarderController, host: str, port: int, web_root: Path) -> ThreadingHTTPServer:
    handler = type("ConfiguredControlHandler", (ControlHandler,), {})
    handler.controller = controller
    handler.web_root = web_root
    return ThreadingHTTPServer((host, port), handler)


def serve_server(controller: ForwarderController, server: ThreadingHTTPServer) -> None:
    """运行控制面，并保证任何退出路径都会清理转发子进程。"""
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows QQ 转发器本机 Web 控制面")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    lock_path = config.runtime.database_path.parent / "web.lock"
    with SingleInstanceLock(lock_path, "Web 控制面"):
        controller = ForwarderController(args.config)
        web_root = Path(__file__).resolve().parent.parent / "web"
        server = create_server(controller, args.host, args.port, web_root)
        print(f"Web 控制面已启动：http://{args.host}:{args.port}")
        serve_server(controller, server)


if __name__ == "__main__":
    try:
        main()
    except SingleInstanceError as exc:
        raise SystemExit(str(exc)) from None
