from __future__ import annotations

import logging
from pathlib import Path
import re
import time
from typing import Any

from ..config import SourceConfig
from .qq_ui_lock import QQ_UI_LOCK


LOGGER = logging.getLogger(__name__)


def _normal(text: str | None) -> str:
    return " ".join((text or "").split()).strip()


class QqWindowImageReader:
    """通过 QQ 聊天窗口复制最新图片到 Windows 剪贴板。"""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    @staticmethod
    def _desktop() -> Any:
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise RuntimeError("缺少 pywinauto，请先安装 requirements.txt") from exc
        return Desktop(backend="uia")

    @staticmethod
    def _texts(window: Any) -> list[str]:
        values: list[str] = []
        try:
            controls = window.descendants(control_type="Text")
        except Exception:
            return values
        for control in controls:
            try:
                value = _normal(control.window_text())
            except Exception:
                continue
            if value and value not in values:
                values.append(value)
        return values

    @staticmethod
    def _is_qq_process_path(path: str) -> bool:
        """只接受 QQNT 可执行文件，避免把包含 qq.com 的浏览器窗口当成 QQ。"""
        normalized = path.replace("/", "\\").casefold()
        executable = Path(normalized).name
        return executable in {"qq.exe", "qqnt.exe"} and "\\tencent\\qqnt\\" in normalized

    @classmethod
    def _is_qq_window(cls, window: Any) -> bool:
        try:
            process_id = int(window.process_id())
        except Exception:
            try:
                process_id = int(window.element_info.process_id)
            except Exception:
                return False
        try:
            import win32api
            import win32con
            import win32process

            access = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
            handle = win32api.OpenProcess(access, False, process_id)
            try:
                process_path = win32process.GetModuleFileNameEx(handle, 0)
            finally:
                win32api.CloseHandle(handle)
        except Exception:
            return False
        return cls._is_qq_process_path(process_path)

    @staticmethod
    def _is_minimized(window: Any) -> bool:
        try:
            return bool(window.is_minimized())
        except Exception:
            try:
                import win32gui

                return bool(win32gui.IsIconic(int(window.handle)))
            except Exception:
                return False

    @staticmethod
    def _conversation_title_matches(value: str, target: str) -> bool:
        """Match QQ's active title, including suffixes such as ``群名 (2)``."""
        normalized_value = _normal(value)
        normalized_target = _normal(target)
        if normalized_value.casefold() == normalized_target.casefold():
            return True
        return bool(re.fullmatch(
            rf"{re.escape(normalized_target)}\s*[\(（]\d+[\)）]",
            normalized_value,
            flags=re.IGNORECASE,
        ))

    def _group_window(self, group_name: str | None = None) -> Any | None:
        target_group = _normal(group_name or self.config.group_name)
        candidates: list[Any] = []
        for window in self._desktop().windows(visible_only=False):
            try:
                if not self._is_qq_window(window):
                    continue
                if self._area(window) < 300 * 300 and not self._is_minimized(window):
                    continue
                window_rect = window.rectangle()
                window_width = max(1, window_rect.right - window_rect.left)
                title = _normal(window.window_text())
                active_title = self._conversation_title_matches(title, target_group)
                if not active_title:
                    # QQ NT versions expose the active conversation title as
                    # Text, Group, or Button. The current Windows build uses a
                    # Button even though it is rendered as plain header text.
                    for control_type in ("Text", "Group", "Button"):
                        for control in window.descendants(control_type=control_type):
                            if not self._conversation_title_matches(control.window_text(), target_group):
                                continue
                            rect = control.rectangle()
                            if (
                                rect.left >= window_rect.left + window_width * 0.25
                                and rect.bottom >= window_rect.top
                                and rect.top >= window_rect.top - 10
                                and rect.top <= window_rect.top + 120
                            ):
                                active_title = True
                                break
                        if active_title:
                            break
                if active_title:
                    candidates.append(window)
            except Exception:
                continue
        if not candidates:
            return None
        # 优先选择面积较大的会话窗口，避免选中通知弹窗。
        def area(window: Any) -> int:
            try:
                rect = window.rectangle()
                return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            except Exception:
                return 0
        return max(candidates, key=area)

    def _qq_window(self) -> Any | None:
        candidates: list[Any] = []
        for window in self._desktop().windows(visible_only=False):
            try:
                if not self._is_qq_window(window):
                    continue
                if self._area(window) < 300 * 300 and not self._is_minimized(window):
                    continue
                title = _normal(window.window_text()).casefold()
                class_name = _normal(getattr(window.element_info, "class_name", "")).casefold()
                texts = self._texts(window)
                searchable = " ".join([title, class_name, *texts]).casefold()
                if self.config.app_name_contains.casefold() in searchable:
                    candidates.append(window)
            except Exception:
                continue
        if not candidates:
            return None
        return max(candidates, key=lambda window: self._area(window))

    @staticmethod
    def _restore_window(window: Any) -> None:
        try:
            if window.is_minimized():
                window.restore()
                time.sleep(0.35)
        except Exception:
            try:
                window.restore()
                time.sleep(0.35)
            except Exception:
                pass

    @staticmethod
    def _area(window: Any) -> int:
        try:
            rect = window.rectangle()
            return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        except Exception:
            return 0

    def _sidebar_conversation_control(self, window: Any, target_group: str) -> Any | None:
        """Find an exact conversation title in QQ's left sidebar."""
        try:
            window_rect = window.rectangle()
            window_width = max(1, window_rect.right - window_rect.left)
            sidebar_right = window_rect.left + window_width * 0.34
            candidates: list[Any] = []
            for control in window.descendants(control_type="Text"):
                if not self._conversation_title_matches(control.window_text(), target_group):
                    continue
                rect = control.rectangle()
                if (
                    rect.right > rect.left
                    and rect.bottom > rect.top
                    and rect.left < sidebar_right
                    and rect.top >= window_rect.top + 80
                    and rect.bottom <= window_rect.bottom
                ):
                    candidates.append(control)
            if candidates:
                return min(candidates, key=lambda control: (
                    control.rectangle().top,
                    control.rectangle().left,
                ))
        except Exception:
            return None
        return None

    @staticmethod
    def _click_control_center(control: Any) -> None:
        # click_input on QQ's accessible Text/parent Group is unreliable in
        # some NT builds. A physical center click consistently switches chats.
        from pywinauto import mouse

        rect = control.rectangle()
        mouse.click(
            button="left",
            coords=((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2),
        )

    def _sidebar_search_edit(self, window: Any) -> Any | None:
        try:
            window_rect = window.rectangle()
            window_width = max(1, window_rect.right - window_rect.left)
            sidebar_right = window_rect.left + window_width * 0.34
            candidates = []
            for control in window.descendants(control_type="Edit"):
                rect = control.rectangle()
                if (
                    rect.right > rect.left
                    and rect.bottom > rect.top
                    and rect.left < sidebar_right
                    and rect.top <= window_rect.top + 140
                ):
                    candidates.append(control)
            if candidates:
                return min(candidates, key=lambda control: control.rectangle().top)
        except Exception:
            return None
        return None

    def _switch_from_sidebar(self, window: Any, target_group: str) -> bool:
        control = self._sidebar_conversation_control(window, target_group)
        search: Any | None = None
        if control is None:
            search = self._sidebar_search_edit(window)
            if search is None:
                LOGGER.warning("QQ 左侧会话搜索框未暴露给 UI Automation")
                return False
            try:
                search.set_focus()
                search.set_edit_text(target_group)
            except Exception:
                return False
            deadline = time.monotonic() + min(self.config.ui_image_wait_seconds, 4.0)
            while time.monotonic() < deadline:
                time.sleep(0.25)
                control = self._sidebar_conversation_control(window, target_group)
                if control is not None:
                    break
        if control is None:
            return False
        try:
            self._click_control_center(control)
            time.sleep(0.6)
            if search is not None:
                try:
                    search.set_edit_text("")
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def _open_group(self, group_name: str | None = None) -> Any | None:
        target_group = _normal(group_name or self.config.group_name)
        window = self._group_window(target_group)
        if window is not None:
            self._restore_window(window)
            try:
                window.set_focus()
            except Exception:
                pass
            return window
        window = self._qq_window()
        if window is None:
            LOGGER.warning("未找到可操作的 QQ 主窗口，无法自动打开群聊：%s", target_group)
            return None
        self._restore_window(window)
        try:
            window.set_focus()
        except Exception:
            pass
        # QQ NT may expose an empty UIA tree while covered by another app.
        # Give Chromium accessibility a moment to rebuild after activation.
        time.sleep(0.45)
        # A minimized QQ window exposes almost no descendants. Check again
        # after restoring and focusing before touching the sidebar.
        active_window = self._group_window(target_group)
        if active_window is not None:
            try:
                active_window.set_focus()
            except Exception:
                pass
            return active_window
        try:
            if not self._switch_from_sidebar(window, target_group):
                LOGGER.warning("QQ 左侧会话列表无法切换到目标会话 group=%s", target_group)
                return None
        except Exception as exc:
            LOGGER.warning("自动打开 QQ 群聊失败 group=%s error=%s", target_group, type(exc).__name__)
            return None
        deadline = time.monotonic() + self.config.ui_image_wait_seconds
        while time.monotonic() < deadline:
            found = self._group_window(target_group)
            if found is not None:
                try:
                    found.set_focus()
                except Exception:
                    pass
                return found
            time.sleep(0.4)
        LOGGER.warning("自动打开 QQ 群聊超时 group=%s", target_group)
        return None

    @staticmethod
    def _image_controls(window: Any) -> list[Any]:
        controls: list[Any] = []
        try:
            for control in window.descendants(control_type="Image"):
                try:
                    rect = control.rectangle()
                    width = rect.right - rect.left
                    height = rect.bottom - rect.top
                    if width >= 40 and height >= 40:
                        controls.append(control)
                except Exception:
                    continue
        except Exception:
            return controls
        return controls

    def _copy_from_context_menu(self, image: Any) -> bool:
        """通过 QQ 图片右键菜单复制，兼容 QQ NT 不响应直接 Ctrl+C 的情况。"""
        try:
            image.click_input(button="right")
            time.sleep(0.35)
            copy_labels = {"复制图片", "复制"}
            for menu_window in self._desktop().windows(visible_only=True):
                for item in menu_window.descendants(control_type="MenuItem"):
                    try:
                        label = _normal(item.window_text())
                    except Exception:
                        continue
                    if label in copy_labels:
                        item.click_input()
                        return True
            # 没有识别到菜单项时关闭菜单，交给直接复制逻辑重试。
            image.type_keys("{ESC}")
        except Exception:
            try:
                image.type_keys("{ESC}")
            except Exception:
                pass
        return False

    def _copy_control_to_file(
        self,
        window: Any,
        image: Any,
        target: Path,
        group_name: str | None = None,
    ) -> Path | None:
        try:
            import win32clipboard
            clipboard_sequence = win32clipboard.GetClipboardSequenceNumber()
        except Exception:
            clipboard_sequence = None
        try:
            copied_by_menu = self._copy_from_context_menu(image)
            if not copied_by_menu:
                image.click_input()
                window.type_keys("^c", set_foreground=True)
        except Exception as exc:
            LOGGER.warning("复制 QQ 图片到剪贴板失败 group=%s error=%s", group_name or self.config.group_name, type(exc).__name__)
            return None

        try:
            from PIL import ImageGrab
        except ImportError:
            LOGGER.warning("缺少 Pillow，无法读取 Windows 图片剪贴板")
            return None
        deadline = time.monotonic() + min(self.config.ui_image_wait_seconds, 5.0)
        while time.monotonic() < deadline:
            try:
                if clipboard_sequence is not None:
                    import win32clipboard
                    if win32clipboard.GetClipboardSequenceNumber() == clipboard_sequence:
                        time.sleep(0.15)
                        continue
                image_data = ImageGrab.grabclipboard()
                if image_data is not None and hasattr(image_data, "save"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    image_data.save(target, "PNG")
                    LOGGER.info("已从 QQ 窗口复制图片到暂存文件 path=%s size=%d", target, target.stat().st_size)
                    return target
            except Exception:
                pass
            time.sleep(0.25)
        return None

    def _capture_many_unlocked(
        self,
        message_keys: list[str],
        directory: Path,
        group_name: str | None = None,
    ) -> list[Path | None]:
        """按消息顺序复制最近的多张图片，避免聚合通知只复制最后一张。"""
        if not message_keys:
            return []
        target_group = _normal(group_name or self.config.group_name)
        window = self._open_group(target_group)
        if window is None:
            return [None for _ in message_keys]
        try:
            controls = self._image_controls(window)
            if not controls:
                LOGGER.warning("QQ A 群窗口未找到可复制的 Image 控件 group=%s", target_group)
                return [None for _ in message_keys]
            controls.sort(key=lambda control: (control.rectangle().bottom, control.rectangle().right))
            selected = controls[-len(message_keys):]
            selected = list(selected)
            if len(selected) < len(message_keys):
                selected = [None] * (len(message_keys) - len(selected)) + selected
            LOGGER.info("QQ A 群找到图片控件 count=%d，准备复制=%d", len(controls), len(message_keys))
        except Exception as exc:
            LOGGER.warning("读取 QQ 图片控件失败 group=%s error=%s", target_group, type(exc).__name__)
            return [None for _ in message_keys]

        results: list[Path | None] = []
        for message_key, image in zip(message_keys, selected):
            if image is None:
                results.append(None)
                continue
            target = directory / f"{message_key[:24]}.png"
            result = self._copy_control_to_file(window, image, target, target_group)
            if result is None:
                LOGGER.warning("QQ 图片复制失败 key=%s group=%s", message_key[:12], target_group)
            results.append(result)
        return results

    def capture_many(
        self,
        message_keys: list[str],
        directory: Path,
        group_name: str | None = None,
    ) -> list[Path | None]:
        with QQ_UI_LOCK:
            return self._capture_many_unlocked(message_keys, directory, group_name)

    def capture(self, message_key: str, directory: Path, group_name: str | None = None) -> Path | None:
        """复制最新可见图片并保存；失败返回 None。"""
        return self.capture_many([message_key], directory, group_name)[0]
