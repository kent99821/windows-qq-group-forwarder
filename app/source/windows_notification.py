from __future__ import annotations

import hashlib
import logging
from collections import Counter, deque
import threading
import time
from typing import Any
from uuid import uuid4

from ..config import SourceConfig
from ..models import IncomingMessage

LOGGER = logging.getLogger(__name__)
RECENT_MESSAGE_SECONDS = 15.0
ACTION_TEXTS = {"关闭", "设置", "回复", "查看", "更多选项"}
Candidate = tuple[str, str, str, str, str]


def normalize_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


class _UserNotificationListenerBackend:
    """Read Windows toast history through the WinRT UserNotificationListener API."""

    def __init__(self, config: SourceConfig) -> None:
        try:
            from winrt.windows.ui.notifications import NotificationKinds
            from winrt.windows.ui.notifications.management import (
                UserNotificationListener,
                UserNotificationListenerAccessStatus,
            )
        except ImportError as exc:
            raise RuntimeError("缺少 Windows WinRT 通知依赖") from exc

        self._notification_kinds = NotificationKinds
        self._listener = UserNotificationListener.current
        status = self._listener.get_access_status()
        if status != UserNotificationListenerAccessStatus.ALLOWED:
            status_name = getattr(status, "name", str(status))
            raise RuntimeError(
                f"Windows UserNotificationListener 权限不可用（{status_name}）；将回退 UI Automation"
            )
        self.config = config
        self._event_lock = threading.Lock()
        self._event_candidates: deque[Candidate] = deque()
        self._event_signal = threading.Event()
        self._event_sequence = 0
        self._event_token: Any | None = None
        try:
            self._event_token = self._listener.add_notification_changed(self._on_notification_changed)
            LOGGER.info("Windows NotificationChanged 事件监听已启用")
        except Exception as exc:
            LOGGER.warning("Windows 通知事件监听不可用，将使用快速轮询：%s", type(exc).__name__)

    @staticmethod
    def _notification_texts(notification: Any) -> list[str]:
        values: list[str] = []
        try:
            bindings = notification.notification.visual.bindings
        except Exception:
            return values
        for binding in bindings:
            try:
                text_elements = binding.get_text_elements()
            except Exception:
                continue
            for element in text_elements:
                try:
                    value = normalize_text(element.text)
                except Exception:
                    continue
                # 不能按文本去重：QQ 可能把连续发送的相同正文聚合在同一
                # 条通知中，重复文本分别代表两条消息，必须保留出现次数。
                if value:
                    values.append(value)
        return values

    @staticmethod
    def _app_name(notification: Any) -> str:
        try:
            display_info = notification.app_info.display_info
            return normalize_text(display_info.display_name)
        except Exception:
            return ""

    def _candidate_items_from_notification(self, notification: Any, identity: str) -> list[Candidate]:
        app = self._app_name(notification)
        texts = self._notification_texts(notification)
        if not app or not texts:
            return []
        return WindowsNotificationReader._candidate_items_from_texts(
            self.config,
            identity,
            app,
            texts,
            "UserNotification",
        )

    def _on_notification_changed(self, _sender: Any, args: Any) -> None:
        """在通知变更事件中立即保存内容，避免下一次轮询前被覆盖。"""
        try:
            notification_id = int(args.user_notification_id)
            notification = self._listener.get_notification(notification_id)
            if notification is None:
                return
            app = self._app_name(notification)
            if not app:
                return
            with self._event_lock:
                self._event_sequence += 1
                sequence = self._event_sequence
            identity = f"winrt-event:{app}:{notification_id}:{sequence}"
            candidates = self._candidate_items_from_notification(notification, identity)
            if not candidates:
                return
            with self._event_lock:
                self._event_candidates.extend(candidates)
                self._event_signal.set()
        except Exception:
            # 删除通知等事件可能已经无法读取对应对象，快照轮询仍会兜底。
            return

    def wait_for_change(self, timeout: float) -> None:
        self._event_signal.wait(max(0.0, timeout))
        self._event_signal.clear()

    def close(self) -> None:
        token = self._event_token
        self._event_token = None
        if token is None:
            return
        try:
            self._listener.remove_notification_changed(token)
        except Exception:
            pass

    def scan(self) -> list[Candidate]:
        with self._event_lock:
            if self._event_candidates:
                results = list(self._event_candidates)
                self._event_candidates.clear()
                return results
        try:
            notifications = self._listener.get_notifications_async(
                self._notification_kinds.TOAST
            ).get()
        except Exception as exc:
            raise RuntimeError(f"读取 Windows 通知 API 失败：{type(exc).__name__}") from exc

        results: list[Candidate] = []
        for notification in notifications:
            try:
                notification_id = int(notification.id)
            except (AttributeError, TypeError, ValueError):
                continue
            app = self._app_name(notification)
            identity = f"winrt:{app}:{notification_id}"
            results.extend(self._candidate_items_from_notification(notification, identity))
        return results


class WindowsNotificationReader:
    """优先通过 WinRT API 读取 Windows 通知，必要时回退到 UI Automation。"""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        self._primed = False
        # 保存当前仍可见的弹窗实例。通知窗口每次轮询都会重新包装成
        # pywinauto 对象，因此不能用 Python 对象 id 做去重。
        self._active: set[str] = set()
        # Windows 会在通知栈变化时重建 UIA 控件。即使正文没变，runtime_id
        # 也可能变化；短时间内容去重用于避免同一 toast 被反复转发。
        self._recent_content: dict[str, float] = {}
        self._last_content_counts: Counter[str] = Counter()
        self._backend_name = "windows-user-notification-listener"
        try:
            self._api_backend: _UserNotificationListenerBackend | None = _UserNotificationListenerBackend(config)
        except RuntimeError as exc:
            LOGGER.warning("Windows 通知 API 不可用：%s", exc)
            self._api_backend = None
            self._backend_name = "windows-toast-uia-fallback"

    def _windows(self) -> list[Any]:
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise RuntimeError("缺少 pywinauto，请先安装 requirements.txt") from exc
        return Desktop(backend="uia").windows(visible_only=True)

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def wait_for_change(self, timeout: float) -> None:
        if self._api_backend is not None:
            self._api_backend.wait_for_change(timeout)
            return
        time.sleep(max(0.0, timeout))

    def close(self) -> None:
        if self._api_backend is not None:
            self._api_backend.close()

    @staticmethod
    def _small_window(window: Any) -> bool:
        try:
            rectangle = window.rectangle()
            width = rectangle.right - rectangle.left
            height = rectangle.bottom - rectangle.top
            return 180 <= width <= 900 and 60 <= height <= 500
        except Exception:
            return True

    @staticmethod
    def _texts(window: Any) -> list[str]:
        values: list[str] = []
        try:
            controls = window.descendants(control_type="Text")
        except Exception:
            return values
        for control in controls:
            try:
                value = normalize_text(control.window_text())
            except Exception:
                continue
            # 不能按正文去重：连续两条相同的图片消息会由 Windows
            # 聚合为两个内容完全相同的 Text 控件。
            if value:
                values.append(value)
        return values

    @staticmethod
    def _identity(window: Any, texts: list[str]) -> str:
        """取得一个在弹窗存活期间稳定的 UIA/窗口标识。"""
        try:
            runtime_id = getattr(window.element_info, "runtime_id", None)
            if runtime_id:
                return f"runtime:{runtime_id}"
        except Exception:
            pass
        try:
            handle = int(getattr(window, "handle", 0) or 0)
            if handle:
                return f"hwnd:{handle}"
        except Exception:
            pass
        return "fallback:" + hashlib.sha256("\0".join(texts).encode("utf-8")).hexdigest()

    @staticmethod
    def _candidate_items_from_texts(
        config: SourceConfig,
        identity: str,
        title: str,
        texts: list[str],
        class_name: str,
    ) -> list[Candidate]:
        if not texts:
            return []
        searchable = " ".join([title, class_name, *texts]).casefold()
        app_match = config.app_name_contains.casefold() in searchable
        # QQ 的不同版本对通知来源名称暴露方式不同。有的把 QQ 暴露为
        # Text，有的只暴露 Windows toast 的 CoreWindow 类名；两者都允许。
        core_window = class_name.casefold() == "windows.ui.core.corewindow"
        if not app_match and not core_window:
            return []
        configured_names = {
            normalize_text(name).casefold(): normalize_text(name)
            for name in config.listener_names
            if normalize_text(name)
        }
        group_indexes = [
            (index, configured_names[normalize_text(text).casefold()])
            for index, text in enumerate(texts)
            if normalize_text(text).casefold() in configured_names
        ]
        if not group_indexes:
            return []
        excluded = {item.casefold() for item in config.exclude_texts}
        bodies: list[tuple[str, str]] = []
        for group_index, group_name in group_indexes:
            # Windows 可能把多个 QQ toast 聚合到同一个顶层窗口。会话名称后面
            # 连续的多段消息都属于该会话；遇到下一个已配置的会话名称时停止，
            # 避免把其他会话的消息混入。
            for text in texts[group_index + 1:]:
                value = normalize_text(text)
                folded = value.casefold()
                if not value:
                    continue
                if folded in configured_names:
                    break
                if config.app_name_contains.casefold() in folded:
                    continue
                if folded in excluded or value in ACTION_TEXTS:
                    continue
                bodies.append((group_name, value))
        app = title or config.app_name_contains
        return [
            (f"{identity}:item:{index}", app, group_name, body, class_name)
            for index, (group_name, body) in enumerate(bodies)
        ]

    def _candidate_items(self, window: Any) -> list[Candidate]:
        if not self._small_window(window):
            return []
        texts = self._texts(window)
        if not texts:
            return []
        title = normalize_text(window.window_text())
        class_name = normalize_text(getattr(window.element_info, "class_name", ""))
        return self._candidate_items_from_texts(
            self.config,
            self._identity(window, texts),
            title,
            texts,
            class_name,
        )

    def _candidate(self, window: Any) -> Candidate | None:
        items = self._candidate_items(window)
        return items[0] if items else None

    def _scan_uia(self) -> list[Candidate]:
        results: list[Candidate] = []
        for window in self._windows():
            try:
                candidates = self._candidate_items(window)
            except Exception:
                continue
            for candidate in candidates:
                if candidate not in results:
                    results.append(candidate)
        return results

    def _scan(self) -> list[Candidate]:
        if self._api_backend is not None:
            try:
                api_results = self._api_backend.scan()
                if api_results:
                    return api_results
                # 某些 QQ 桌面版本会显示 Windows Toast，但不会把该 Toast
                # 暴露给 UserNotificationListener；此时必须读取可见通知窗口。
                uia_results = self._scan_uia()
                if uia_results:
                    self._backend_name = "windows-user-notification-listener+uia-fallback"
                    return uia_results
                return []
            except RuntimeError as exc:
                LOGGER.warning("Windows 通知 API 读取失败，将回退 UI Automation：%s", exc)
                self._api_backend = None
                self._backend_name = "windows-toast-uia-fallback"
        return self._scan_uia()

    def _fingerprint(self, identity: str, app: str, body: str) -> str:
        return hashlib.sha256(f"{identity}\0{app}\0{self.config.listener_names}\0{body}".encode("utf-8")).hexdigest()

    def _content_fingerprint(self, app: str, group_name: str, body: str) -> str:
        return hashlib.sha256(f"{app}\0{group_name}\0{body}".encode("utf-8")).hexdigest()

    def _message(
        self,
        identity: str,
        app: str,
        body: str | None = None,
        source_group: str | None = None,
    ) -> IncomingMessage:
        # 保留旧的两参数诊断调用兼容性；实时监听始终传入三个参数。
        if body is None:
            body = app
            app = identity
            identity = "manual"
        # 去重由 _active 负责；这里使用随机事件 key，允许“相同正文的
        # 两条新消息”在弹窗消失后再次转发。
        fingerprint = self._fingerprint(identity, app, body)
        key = hashlib.sha256(f"{fingerprint}\0{uuid4().hex}".encode("ascii")).hexdigest()
        kind = "toast_image_notice" if "[图片]" in body else "toast_text"
        # 联系人通知通常只有“联系人昵称 + 正文”，没有“发送者：正文”前缀。
        # 这时来源会话名称就是应显示在转发消息中的联系人昵称；群通知
        # 若带有发送者前缀则保留原始正文，避免重复添加群名。
        sender = source_group if source_group and ":" not in body and "：" not in body else None
        return IncomingMessage.create(
            key,
            source_group or self.config.group_name,
            body,
            sender=sender,
            kind=kind,
        )

    def prime(self) -> None:
        try:
            now = time.monotonic()
            scanned = self._scan()
            self._active = {
                self._fingerprint(identity, app, body)
                for identity, app, _group_name, body, _class_name in scanned
            }
            self._recent_content = {
                self._content_fingerprint(app, group_name, body): now + RECENT_MESSAGE_SECONDS
                for _identity, app, group_name, body, _class_name in scanned
            }
            self._last_content_counts = Counter(
                self._content_fingerprint(app, group_name, body)
                for _identity, app, group_name, body, _class_name in scanned
            )
            self._primed = True
        except RuntimeError as exc:
            LOGGER.warning("Windows 通知基线初始化失败：%s", exc)
            self._primed = False

    def poll(self) -> list[IncomingMessage]:
        now = time.monotonic()
        self._recent_content = {
            key: expires for key, expires in self._recent_content.items()
            if expires > now
        }
        scanned = self._scan()
        current_content_counts = Counter(
            self._content_fingerprint(app, group_name, body)
            for _identity, app, group_name, body, _class_name in scanned
        )
        # 如果上一次扫描中已经没有某种正文，下一次出现相同正文应视为新消息。
        for key in list(self._recent_content):
            if self._last_content_counts.get(key, 0) == 0:
                self._recent_content.pop(key, None)
        recent_before = set(self._recent_content)
        content_increase = {
            key: max(0, count - self._last_content_counts.get(key, 0))
            for key, count in current_content_counts.items()
        }
        emitted_by_content: Counter[str] = Counter()
        emitted_content_keys: set[str] = set()
        current = {
            self._fingerprint(identity, app, body)
            for identity, app, _group_name, body, _class_name in scanned
        }
        found: list[IncomingMessage] = []
        for identity, app, group_name, body, _class_name in scanned:
            fingerprint = self._fingerprint(identity, app, body)
            content_fingerprint = self._content_fingerprint(app, group_name, body)
            if fingerprint in self._active:
                continue
            if content_fingerprint in recent_before:
                # 同一通知窗口被重建时不重复发送；但如果聚合数量增加，
                # 允许新增的那几条相同正文进入队列。
                if emitted_by_content[content_fingerprint] >= content_increase.get(content_fingerprint, 0):
                    continue
            found.append(self._message(identity, app, body, source_group=group_name))
            emitted_by_content[content_fingerprint] += 1
            emitted_content_keys.add(content_fingerprint)
        for content_fingerprint in emitted_content_keys:
            self._recent_content[content_fingerprint] = now + RECENT_MESSAGE_SECONDS
        self._active = current
        self._last_content_counts = current_content_counts
        if not self._primed:
            self._primed = True
            return []
        return found

    def inspect(self) -> dict[str, object]:
        if self._api_backend is not None:
            scanned = self._scan()
            notifications = [
                {
                    "window_title": app,
                    "class_name": class_name,
                    "identity": identity,
                    "texts": [group_name, body],
                    "body": body,
                }
                for identity, app, group_name, body, class_name in scanned
            ]
            return {"backend": self._backend_name, "notifications": notifications}

        notifications: list[dict[str, object]] = []
        for window in self._windows():
            if not self._small_window(window):
                continue
            texts = self._texts(window)
            if not texts:
                continue
            candidate = self._candidate(window)
            if candidate is None:
                continue
            identity, title, group_name, body, class_name = candidate
            notifications.append({
                "window_title": title,
                "class_name": class_name,
                "identity": identity,
                "texts": texts,
                "group_name": group_name,
                "body": body,
            })
        return {"backend": self._backend_name, "notifications": notifications}
