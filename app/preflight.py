from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from .config import AppConfig, load_config
from .destination.qq_bot import OfficialQqBotSender
from .source.qq_image_cache import QqImageCache
from .source.qq_window_image import QqWindowImageReader
from .source.windows_notification import WindowsNotificationReader
from .state_store import StateStore


PLACEHOLDER_MARKERS = ("替换", "example", "your_", "填写", "appid", "group_openid")


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


async def _check_bot_credentials(config: AppConfig) -> None:
    sender = OfficialQqBotSender(config.destination)
    try:
        await sender.start()
    finally:
        await sender.close()


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

    if config.source.listener_names:
        _append(
            report,
            "passed",
            "listeners",
            "监听会话",
            f"已配置 {len(config.source.listener_names)} 个群或联系人",
        )
    else:
        _append(report, "missing", "listeners", "监听会话", "至少添加一个 QQ 群或联系人")

    if config.source.app_name_contains.strip():
        _append(report, "passed", "app_name", "QQ 应用识别规则", f"通知应用名包含“{config.source.app_name_contains}”")
    else:
        _append(report, "missing", "app_name", "QQ 应用识别规则", "app_name_contains 不能为空")

    app_id_valid = not _looks_like_placeholder(config.destination.app_id)
    group_valid = not _looks_like_placeholder(config.destination.group_openid)
    if app_id_valid:
        _append(report, "passed", "app_id", "机器人 AppID", "已填写")
    else:
        _append(report, "missing", "app_id", "机器人 AppID", "尚未填写有效的 AppID")
    if group_valid:
        value = config.destination.group_openid
        preview = f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "已填写"
        _append(report, "passed", "group_openid", "B 群绑定", preview)
    else:
        _append(report, "missing", "group_openid", "B 群绑定", "尚未绑定 B 群 group_openid")

    secret = os.environ.get(config.destination.client_secret_env)
    if secret:
        _append(
            report,
            "passed",
            "client_secret",
            "机器人密钥",
            f"已读取环境变量 {config.destination.client_secret_env}",
        )
    elif config.runtime.dry_run:
        _append(
            report,
            "warnings",
            "client_secret",
            "机器人密钥",
            f"Dry-run 可继续；真实发送前需设置 {config.destination.client_secret_env}",
        )
    else:
        _append(
            report,
            "missing",
            "client_secret",
            "机器人密钥",
            f"当前 Web 控制面未读取环境变量 {config.destination.client_secret_env}",
        )

    if os.name == "nt" and sys.platform == "win32":
        _append(report, "passed", "windows", "Windows 环境", "当前正在 Windows 中运行")
    else:
        _append(report, "missing", "windows", "Windows 环境", "此项目只能在 Windows 中运行")

    missing_dependencies: list[str] = []
    required_modules = ["httpx", "pywinauto", "PIL"]
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

    if os.name == "nt":
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

    can_verify_bot = bool(secret and app_id_valid)
    if verify_remote and can_verify_bot:
        try:
            asyncio.run(_check_bot_credentials(config))
            _append(report, "passed", "bot_connection", "机器人连接", "AppID 和密钥验证成功")
        except Exception as exc:
            target = "warnings" if config.runtime.dry_run else "missing"
            _append(report, target, "bot_connection", "机器人连接", f"验证失败：{exc}")
    elif not verify_remote:
        _append(report, "warnings", "bot_connection", "机器人连接", "本次未执行联网验证")

    return {**report, "ready": not report["missing"]}
