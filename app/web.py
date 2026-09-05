from __future__ import annotations

import argparse
import asyncio
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
from .config import AppConfig, load_config, save_group_openid
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

    def config(self) -> AppConfig:
        return load_config(self.config_path)

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _reap(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            self.last_exit_code = self.process.returncode
            self.process = None

    def status(self) -> dict[str, Any]:
        with self.lock:
            self._reap()
            config_exists = self.config_path.exists()
            summary = {"pending": 0, "sent": 0}
            config_error = None
            if config_exists:
                try:
                    config = self.config()
                    store = StateStore(config.runtime.database_path)
                    try:
                        summary = store.summary()
                    finally:
                        store.close()
                except Exception as exc:
                    config_error = str(exc)
            return {
                "running": self._alive(),
                "pid": self.process.pid if self._alive() and self.process else None,
                "started_at": self.started_at,
                "last_exit_code": self.last_exit_code,
                "last_action_error": self.last_action_error,
                "config_path": str(self.config_path),
                "config_exists": config_exists,
                "config_error": config_error,
                "messages": summary,
            }

    def start(self, dry_run: bool | None = None) -> dict[str, Any]:
        with self.lock:
            self._reap()
            self.last_action_error = None
            if self._alive():
                return self.status()
            config = self.config()
            effective_dry_run = config.runtime.dry_run if dry_run is None else dry_run
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
            return self.status()

    def restart(self, dry_run: bool | None = None) -> dict[str, Any]:
        self.stop()
        return self.start(dry_run=dry_run)

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
            elif path == "/api/actions/inspect-window":
                result = self.controller.inspect_window()
            elif path == "/api/actions/inspect-image-cache":
                result = self.controller.inspect_image_cache()
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
