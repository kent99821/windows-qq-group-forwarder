from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any

from ..config import SourceConfig


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

    def _group_window(self) -> Any | None:
        candidates: list[Any] = []
        for window in self._desktop().windows(visible_only=True):
            try:
                if not self._is_qq_window(window):
                    continue
                if self._area(window) < 300 * 300:
                    continue
                title = _normal(window.window_text())
                searchable = " ".join([title, *self._texts(window)]).casefold()
                if self.config.group_name.casefold() in searchable:
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
        for window in self._desktop().windows(visible_only=True):
            try:
                if not self._is_qq_window(window):
                    continue
                if self._area(window) < 300 * 300:
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
    def _area(window: Any) -> int:
        try:
            rect = window.rectangle()
            return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        except Exception:
            return 0

    def _open_group(self) -> Any | None:
        window = self._group_window()
        if window is not None:
            try:
                window.set_focus()
            except Exception:
                pass
            return window
        window = self._qq_window()
        if window is None:
            LOGGER.warning("未找到可操作的 QQ 主窗口，无法自动打开群聊：%s", self.config.group_name)
            return None
        try:
            window.set_focus()
            # QQ NT 常用 Ctrl+K 打开全局搜索；如果当前版本不支持，
            # 后续仍会记录失败并回退，不会操作其他群聊。
            window.type_keys("^k", set_foreground=True)
            time.sleep(0.4)
            edits = window.descendants(control_type="Edit")
            if not edits:
                LOGGER.warning("QQ 搜索框未暴露给 UI Automation")
                return None
            search = edits[-1]
            search.set_focus()
            search.set_edit_text(self.config.group_name)
            search.type_keys("{ENTER}", set_foreground=True)
        except Exception as exc:
            LOGGER.warning("自动打开 QQ 群聊失败 group=%s error=%s", self.config.group_name, type(exc).__name__)
            return None
        deadline = time.monotonic() + self.config.ui_image_wait_seconds
        while time.monotonic() < deadline:
            found = self._group_window()
            if found is not None:
                try:
                    found.set_focus()
                except Exception:
                    pass
                return found
            time.sleep(0.4)
        LOGGER.warning("自动打开 QQ 群聊超时 group=%s", self.config.group_name)
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

    def _copy_control_to_file(self, window: Any, image: Any, target: Path) -> Path | None:
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
            LOGGER.warning("复制 QQ 图片到剪贴板失败 group=%s error=%s", self.config.group_name, type(exc).__name__)
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

    def capture_many(self, message_keys: list[str], directory: Path) -> list[Path | None]:
        """按消息顺序复制最近的多张图片，避免聚合通知只复制最后一张。"""
        if not message_keys:
            return []
        window = self._open_group()
        if window is None:
            return [None for _ in message_keys]
        try:
            controls = self._image_controls(window)
            if not controls:
                LOGGER.warning("QQ A 群窗口未找到可复制的 Image 控件 group=%s", self.config.group_name)
                return [None for _ in message_keys]
            controls.sort(key=lambda control: (control.rectangle().bottom, control.rectangle().right))
            selected = controls[-len(message_keys):]
            selected = list(selected)
            if len(selected) < len(message_keys):
                selected = [None] * (len(message_keys) - len(selected)) + selected
            LOGGER.info("QQ A 群找到图片控件 count=%d，准备复制=%d", len(controls), len(message_keys))
        except Exception as exc:
            LOGGER.warning("读取 QQ 图片控件失败 group=%s error=%s", self.config.group_name, type(exc).__name__)
            return [None for _ in message_keys]

        results: list[Path | None] = []
        for message_key, image in zip(message_keys, selected):
            if image is None:
                results.append(None)
                continue
            target = directory / f"{message_key[:24]}.png"
            result = self._copy_control_to_file(window, image, target)
            if result is None:
                LOGGER.warning("QQ 图片复制失败 key=%s group=%s", message_key[:12], self.config.group_name)
            results.append(result)
        return results

    def capture(self, message_key: str, directory: Path) -> Path | None:
        """复制最新可见图片并保存；失败返回 None。"""
        return self.capture_many([message_key], directory)[0]
