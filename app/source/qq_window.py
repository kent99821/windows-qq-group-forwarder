from __future__ import annotations

from collections import Counter
import hashlib
import logging
import re
from typing import Any

from ..config import SourceConfig
from ..models import IncomingMessage

LOGGER = logging.getLogger(__name__)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class QqWindowReader:
    """通过 pywinauto/UIA 读取可见文本；QQ 版本差异由这里隔离。"""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        self._total_counts: Counter[str] = Counter()
        self._primed = False

    def _window(self) -> Any:
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise RuntimeError("缺少 pywinauto，请先安装 requirements.txt") from exc
        desktop = Desktop(backend="uia")
        windows = desktop.windows(visible_only=True)
        candidates = [
            window for window in windows
            if any(name.casefold() in (window.window_text() or "").casefold() for name in self.config.listener_names)
        ]
        if not candidates and self.config.listener_names:
            # QQ NT 的顶层窗口标题通常只有“QQ”，群名位于内部会话控件中。
            # 此时用 UIA 文本做第二级匹配，兼容截图所示的窗口结构。
            for window in windows:
                try:
                    texts = [
                        normalize_text(control.window_text())
                        for control in window.descendants(control_type="Text")
                    ]
                    if any(
                        name.casefold() in text.casefold()
                        for name in self.config.listener_names
                        for text in texts
                        if text
                    ):
                        candidates.append(window)
                except Exception:
                    continue
        if not candidates:
            titles = [window.window_text() for window in windows if window.window_text()]
            suffix = f"；检测到的窗口：{', '.join(titles[:8])}" if titles else ""
            names = "、".join(self.config.listener_names)
            raise RuntimeError(f"没有找到包含“{names}”的可见 QQ 窗口{suffix}")
        return candidates[0]

    def _texts(self, window: Any) -> list[str]:
        values: list[str] = []
        for control in window.descendants(control_type="Text"):
            try:
                text = normalize_text(control.window_text())
            except Exception:
                continue
            if not text or text in self.config.exclude_texts:
                continue
            if text in self.config.listener_names:
                continue
            # 去掉明显的窗口控件文本；详细规则可在配置中扩展。
            if text in {"发送", "消息", "表情", "图片", "文件", "更多"}:
                continue
            values.append(text)
        return values

    def prime(self) -> None:
        """建立启动基线，不把窗口中已有历史消息当作新消息。"""
        try:
            self._total_counts = Counter(self._texts(self._window()))
            self._primed = True
        except RuntimeError as exc:
            LOGGER.warning("QQ 窗口基线初始化失败：%s", exc)
            self._total_counts = Counter()
            self._primed = False

    def poll(self) -> list[IncomingMessage]:
        window = self._window()
        current = Counter(self._texts(window))
        if not self._primed:
            # QQ 后启动或窗口暂时不可见时，第一次恢复只建立基线，避免补发历史。
            self._total_counts = current
            self._primed = True
            LOGGER.info("QQ 窗口已恢复，已建立新的读取基线")
            return []
        messages: list[IncomingMessage] = []
        for content, count in current.items():
            previous = self._total_counts[content]
            if count <= previous:
                continue
            for ordinal in range(previous + 1, count + 1):
                key_material = f"{self.config.listener_names}\0{content}\0{ordinal}"
                key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
                messages.append(IncomingMessage.create(key, self.config.group_name, content))
        # 只增加计数，不因旧消息滚出窗口而减少，避免窗口滚动造成大范围重放。
        for content, count in current.items():
            self._total_counts[content] = max(self._total_counts[content], count)
        return messages

    def inspect(self) -> dict[str, object]:
        window = self._window()
        result: list[dict[str, object]] = []
        for control in window.descendants():
            try:
                text = normalize_text(control.window_text())
                if not text:
                    continue
                result.append({
                    "control_type": control.element_info.control_type,
                    "automation_id": control.element_info.automation_id,
                    "class_name": control.element_info.class_name,
                    "text": text,
                })
            except Exception:
                continue
        return {"window_title": window.window_text(), "controls": result}
