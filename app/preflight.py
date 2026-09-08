from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig, load_config
from .destination.qq_bot import OfficialQqBotSender
from .environment import read_user_environment_variable
from .source.qq_image_cache import QqImageCache
from .source.qq_window_image import QqWindowImageReader
from .source.windows_notification import WindowsNotificationReader
from .state_store import StateStore


PLACEHOLDER_MARKERS = ("替换", "example", "your_", "填写", "待绑定", "appid", "group_openid")


def _entry(key: str, label: str, detail: str, status: str) -> dict[str, str]:
    return {"key": key, "label": label, "detail": detail, "status": status}


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    return not normalized or any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def _append(
    report: dict[str, list[dict[str, str]]],
    status: str,
    key: str,
    label: str,
    detail: str,
) -> None:
    report[status].append(_entry(key, label, detail, status))


def _check_writable(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="qq-forwarder-check-", dir=path, delete=True):
        pass


async def _check_bot_credentials(destination: Any) -> None:
    sender = OfficialQqBotSender(destination)
    try:
        await sender.start()
    finally:
        await sender.close()


async def _check_napcat_connection(config: AppConfig) -> None:
    """Perform a read-only OneBot login check; never sends a QQ message."""
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("缺少 websockets 依赖") from exc
    token = read_user_environment_variable(config.napcat.token_env)
    headers = {"Authorization": f"Bearer {token}"}
    kwargs = {
        "open_timeout": config.napcat.connect_timeout_seconds,
        "ping_interval": 20,
        "ping_timeout": config.napcat.heartbeat_timeout_seconds,
    }
    try:
        try:
            websocket = await websockets.connect(config.napcat.ws_url, additional_headers=headers, **kwargs)
        except TypeError as exc:
            if "additional_headers" not in str(exc):
                raise
            websocket = await websockets.connect(config.napcat.ws_url, extra_headers=headers, **kwargs)
        try:
            await websocket.send('{"action":"get_login_info","params":{},"echo":"qq-forwarder-preflight"}')
            deadline = asyncio.get_running_loop().time() + config.napcat.heartbeat_timeout_seconds
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise RuntimeError("等待 get_login_info 响应超时")
                raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                try:
                    response = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(response, dict):
                    continue
                if response.get("echo") != "qq-forwarder-preflight":
                    # 心跳、生命周期事件和其他未关联响应不能作为登录检查结果。
                    continue
                if response.get("status") != "ok" or response.get("retcode") not in {0, None} or not isinstance(response.get("data"), dict):
                    raise RuntimeError("NapCat get_login_info 未返回有效登录信息")
                break
        finally:
            await websocket.close()
    except Exception as exc:
        raise RuntimeError(f"NapCat WebSocket 或登录检查失败：{type(exc).__name__}") from exc


def run_preflight(config_path: Path, *, verify_remote: bool = True) -> dict[str, Any]:
    """Check everything required by the Windows forwarder without sending a message."""
    report: dict[str, list[dict[str, str]]] = {"passed": [], "missing": [], "warnings": []}
    config_path = config_path.resolve()
    if not config_path.exists():
        _append(report, "missing", "config", "配置文件", f"未找到 {config_path}")
        return {**report, "ready": False}
    _append(report, "passed", "config", "配置文件", "config.toml 已找到")

    try:
        config = load_config(config_path)
    except Exception as exc:
        _append(report, "missing", "config_parse", "配置内容", str(exc))
        return {**report, "ready": False}
    _append(report, "passed", "config_parse", "配置内容", "配置格式正确")

    if config.source.backend == "napcat":
        if not config.napcat.enabled:
            _append(report, "missing", "napcat_enabled", "NapCat 消息源", "source.backend 已选择 napcat，但 napcat.enabled 未开启")
        else:
            _append(report, "passed", "napcat_enabled", "NapCat 消息源", "NapCat OneBot 消息源已启用")
        if config.source.sessions:
            _append(report, "passed", "listeners", "监听会话", f"已配置 {len(config.source.sessions)} 个带 ID 的群或联系人")
        else:
            _append(report, "missing", "listeners", "监听会话", "NapCat 模式必须至少配置一个 source.sessions 会话 ID")
        parsed_url = urlparse(config.napcat.ws_url)
        if parsed_url.hostname in {"127.0.0.1", "localhost", "::1"}:
            _append(report, "passed", "napcat_url", "NapCat WebSocket 地址", config.napcat.ws_url)
        else:
            _append(report, "warnings", "napcat_url", "NapCat WebSocket 地址", "地址不是本机回环地址，请确认未暴露到公网")
        if read_user_environment_variable(config.napcat.token_env):
            _append(report, "passed", "napcat_token", "NapCat Token", f"已读取当前用户环境变量 {config.napcat.token_env}")
        else:
            _append(report, "missing", "napcat_token", "NapCat Token", f"未设置当前用户环境变量 {config.napcat.token_env}")
    elif config.source.listener_names:
        _append(report, "passed", "listeners", "监听会话", f"已配置 {len(config.source.listener_names)} 个群或联系人")
    else:
        _append(report, "missing", "listeners", "监听会话", "至少添加一个 QQ 群或联系人")

    if config.source.app_name_contains.strip():
        _append(report, "passed", "app_name", "QQ 应用识别规则", f"通知应用名包含“{config.source.app_name_contains}”")
    else:
        _append(report, "missing", "app_name", "QQ 应用识别规则", "app_name_contains 不能为空")

    _append(report, "passed", "destinations", "转发机器人", f"已配置 {len(config.destinations)} 个机器人目标")
    bot_checks: list[tuple[Any, bool, bool, str | None]] = []
    single_destination = len(config.destinations) == 1
    for destination in config.destinations:
        prefix = f"bot:{destination.bot_id}"
        label = f"机器人 {destination.bot_id}"
        key_prefix = "" if single_destination else prefix + ":"
        app_id_valid = not _looks_like_placeholder(destination.app_id)
        group_valid = not _looks_like_placeholder(destination.group_openid)
        secret = read_user_environment_variable(destination.client_secret_env)
        bot_checks.append((destination, app_id_valid, group_valid, secret))
        if app_id_valid:
            _append(report, "passed", f"{key_prefix}app_id", f"{label} AppID", "已填写")
        else:
            _append(report, "missing", f"{key_prefix}app_id", f"{label} AppID", "尚未填写有效的 AppID")
        if group_valid:
            value = destination.group_openid
            preview = f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "已填写"
            _append(report, "passed", f"{key_prefix}group_openid", f"{label} QQ 群绑定", preview)
        else:
            _append(report, "missing", f"{key_prefix}group_openid", f"{label} QQ 群绑定", "尚未绑定 QQ 群 group_openid")
        if secret:
            _append(
                report, "passed", f"{key_prefix}client_secret", f"{label}密钥",
                f"已读取环境变量 {destination.client_secret_env}",
            )
        elif config.runtime.dry_run:
            _append(
                report, "warnings", f"{key_prefix}client_secret", f"{label}密钥",
                f"Dry-run 可继续；真实发送前需设置 {destination.client_secret_env}",
            )
        else:
            _append(
                report, "missing", f"{key_prefix}client_secret", f"{label}密钥",
                f"当前 Web 控制面未读取环境变量 {destination.client_secret_env}",
            )

    if os.name == "nt" and sys.platform == "win32":
        _append(report, "passed", "windows", "Windows 环境", "当前正在 Windows 中运行")
    else:
        _append(report, "missing", "windows", "Windows 环境", "此项目只能在 Windows 中运行")

    missing_dependencies: list[str] = []
    required_modules = ["httpx", "PIL"]
    if config.source.backend == "windows_notification":
        required_modules.append("pywinauto")
    else:
        required_modules.append("websockets")
    if not config.runtime.dry_run:
        required_modules.append("qqbot_agent_sdk")
    for module_name in required_modules:
        try:
            importlib.import_module(module_name)
        except Exception:
            missing_dependencies.append(module_name)
    if missing_dependencies:
        _append(
            report,
            "missing",
            "dependencies",
            "运行依赖",
            "缺少：" + "、".join(missing_dependencies),
        )
    else:
        _append(report, "passed", "dependencies", "运行依赖", "所需 Python 组件已安装")
    try:
        importlib.import_module("winrt.windows.ui.notifications.management")
    except Exception:
        _append(report, "warnings", "winrt", "Windows 通知 API", "WinRT 组件不可用，将使用 UI Automation 通知轮询")

    try:
        _check_writable(config.runtime.database_path.parent)
        _check_writable(config.runtime.log_path.parent)
        store = StateStore(config.runtime.database_path)
        store.close()
        _append(report, "passed", "storage", "数据与日志目录", "目录可写，数据库可打开")
    except Exception as exc:
        _append(report, "missing", "storage", "数据与日志目录", f"无法写入：{exc}")

    if os.name == "nt" and config.source.backend == "windows_notification":
        try:
            qq_window = QqWindowImageReader(config.source)._qq_window()
            if qq_window is None:
                _append(
                    report,
                    "warnings",
                    "qq_window",
                    "QQ 客户端",
                    "未找到 QQ NT 主窗口；文本通知仍可监听，但历史补读和图片复制不可用",
                )
            else:
                _append(report, "passed", "qq_window", "QQ 客户端", "已找到 QQ NT 主窗口")
        except Exception as exc:
            _append(report, "missing", "qq_window", "QQ 客户端", f"检查失败：{exc}")

        reader: WindowsNotificationReader | None = None
        try:
            reader = WindowsNotificationReader(config.source)
            backend = reader.backend_name
            if backend == "windows-user-notification-listener":
                _append(report, "passed", "notifications", "Windows 通知监听", "UserNotificationListener 可用")
            else:
                _append(
                    report,
                    "warnings",
                    "notifications",
                    "Windows 通知监听",
                    "系统通知 API 不可用，当前会回退到弹窗快速轮询",
                )
        except Exception as exc:
            _append(report, "missing", "notifications", "Windows 通知监听", f"不可用：{exc}")
        finally:
            if reader is not None:
                reader.close()

        try:
            cache = QqImageCache(config.source).inspect()
            roots = cache.get("roots", [])
            if roots:
                _append(report, "passed", "image_cache", "QQ 图片缓存", f"已找到 {len(roots)} 个缓存目录")
            else:
                _append(report, "warnings", "image_cache", "QQ 图片缓存", "未找到缓存目录；文本仍可转发，图片可能只能发送占位提示")
        except Exception as exc:
            _append(report, "warnings", "image_cache", "QQ 图片缓存", f"检查失败：{exc}")

    if config.source.backend == "napcat" and verify_remote and read_user_environment_variable(config.napcat.token_env):
        try:
            asyncio.run(_check_napcat_connection(config))
            _append(report, "passed", "napcat_connection", "NapCat 连接与登录", "WebSocket 可连接，QQ 登录信息读取成功")
        except Exception as exc:
            _append(report, "missing", "napcat_connection", "NapCat 连接与登录", str(exc))
    elif config.source.backend == "napcat" and not verify_remote:
        _append(report, "warnings", "napcat_connection", "NapCat 连接与登录", "本次未执行联网验证")

    for destination, app_id_valid, _group_valid, secret in bot_checks:
        key = "bot_connection" if single_destination else f"bot:{destination.bot_id}:connection"
        label = f"机器人 {destination.bot_id} 连接"
        if verify_remote and secret and app_id_valid:
            try:
                asyncio.run(_check_bot_credentials(destination))
                _append(report, "passed", key, label, "AppID 和密钥验证成功")
            except Exception as exc:
                target = "warnings" if config.runtime.dry_run else "missing"
                _append(report, target, key, label, f"验证失败：{exc}")
        elif not verify_remote:
            _append(report, "warnings", key, label, "本次未执行联网验证")

    return {**report, "ready": not report["missing"]}
