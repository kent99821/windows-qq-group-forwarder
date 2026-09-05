from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import logging
import re
import time
from typing import Any, Iterable

from ..config import SourceConfig
from ..models import IncomingMessage
from .qq_ui_lock import QQ_UI_LOCK
from .qq_window_image import QqWindowImageReader, _normal


LOGGER = logging.getLogger(__name__)
TIME_PATTERN = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")
SENDER_PATTERN = re.compile(r"^([^:：]{1,80})\s*[:：]\s*(.*)$", re.DOTALL)
NOTIFICATION_BADGE_PATTERN = re.compile(r"^\[[^\]\r\n]{1,30}\]\s*")
HISTORY_SETTLE_SECONDS = 1.5
HISTORY_SETTLE_INTERVAL_SECONDS = 0.3


@dataclass(frozen=True)
class UiNode:
    control_type: str
    text: str
    left: int
    top: int
    right: int
    bottom: int
    children: tuple["UiNode", ...] = ()


@dataclass(frozen=True)
class HistoryRecord:
    source_group: str
    sender: str | None
    content: str
    display_time: str
    kind: str
    occurrence: int


def _node_texts(node: UiNode) -> list[str]:
    values: list[str] = []
    for child in node.children:
        if child.control_type == "Text":
            value = _normal(child.text)
            if value:
                values.append(value)
        values.extend(_node_texts(child))
    return values


def _contains_image(node: UiNode) -> bool:
    return any(
        child.control_type == "Image" or _contains_image(child)
        for child in node.children
    )


def _inside(rect: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = rect
    outer_left, outer_top, outer_right, outer_bottom = outer
    return (
        left >= outer_left
        and top >= outer_top
        and right <= outer_right
        and bottom <= outer_bottom
    )


def parse_history_nodes(
    nodes: Iterable[UiNode],
    chat_left: int,
    chat_right: int,
    source_group: str,
) -> list[HistoryRecord]:
    """Parse QQ NT chat-list children into ordered incoming messages.

    QQ exposes each visible row as a direct child of the chat list: a wide
    time separator, a small avatar/sender group, then a left or right message
    bubble. Only left-side bubbles are incoming messages.
    """
    width = max(1, chat_right - chat_left)
    midpoint = chat_left + width / 2
    current_sender: str | None = None
    current_time = ""
    message_containers: list[tuple[int, int, int, int]] = []
    occurrences: Counter[tuple[str, str, str]] = Counter()
    records: list[HistoryRecord] = []

    for node in nodes:
        rect = (node.left, node.top, node.right, node.bottom)
        node_width = node.right - node.left
        node_height = node.bottom - node.top
        if node_width <= 0 or node_height <= 0:
            continue
        if node.right <= chat_left or node.left >= chat_right:
            continue

        text = _normal(node.text)
        child_texts = _node_texts(node)
        time_value = next((value for value in child_texts if TIME_PATTERN.fullmatch(value)), None)

        # Time separators span most of the conversation width and are not
        # message bubbles even though they contain a Text child.
        if node.control_type == "Group" and node_width >= width * 0.7 and time_value:
            current_time = time_value
            message_containers.append(rect)
            continue

        center_x = (node.left + node.right) / 2
        incoming_side = center_x < midpoint

        # QQ NT commonly exposes the sender/avatar as a small childless Group
        # whose accessible name is the sender nickname.
        if (
            node.control_type == "Group"
            and incoming_side
            and text
            and not node.children
            and node_width <= 100
            and node_height <= 100
        ):
            current_sender = text
            continue

        if node.control_type == "Text":
            if TIME_PATTERN.fullmatch(text):
                current_time = text
                continue
            # descendants() may expose a bubble's Text child again after its
            # parent. It is content, not the sender of the next message.
            if any(_inside(rect, container) for container in message_containers):
                continue
            if incoming_side and text and node_width <= width * 0.45:
                current_sender = text
            continue

        if node.control_type != "Group":
            continue

        # Remember both incoming and outgoing bubble rectangles so their Text
        # descendants cannot later be mistaken for sender labels.
        if node.children:
            message_containers.append(rect)
        if not incoming_side:
            continue

        kind = "history_text"
        if _contains_image(node):
            content = "[图片]"
            kind = "toast_image_notice"
        else:
            content_parts = [value for value in child_texts if not TIME_PATTERN.fullmatch(value)]
            content = _normal(" ".join(content_parts))
            if not content and text and node_width > 100:
                content = text
        if not content:
            continue

        sender = current_sender or source_group
        occurrence_key = (_normal(sender), content, current_time)
        occurrences[occurrence_key] += 1
        records.append(HistoryRecord(
            source_group=source_group,
            sender=sender,
            content=content,
            display_time=current_time,
            kind=kind,
            occurrence=occurrences[occurrence_key],
        ))
    return records


def _sender_and_content(sender: str | None, content: str) -> tuple[str, str]:
    normalized_content = NOTIFICATION_BADGE_PATTERN.sub("", _normal(content), count=1)
    if sender:
        return _normal(sender).casefold(), normalized_content.casefold()
    matched = SENDER_PATTERN.match(normalized_content)
    if matched:
        return _normal(matched.group(1)).casefold(), _normal(matched.group(2)).casefold()
    return "", normalized_content.casefold()


def _record_key(record: HistoryRecord) -> str:
    material = "\0".join((
        record.source_group,
        record.sender or "",
        record.display_time,
        record.content,
        str(record.occurrence),
    ))
    return hashlib.sha256(f"qq-history\0{material}".encode("utf-8")).hexdigest()


def merge_history_snapshots(
    snapshots: Iterable[Iterable[HistoryRecord]],
) -> list[HistoryRecord]:
    """Union consecutive UI snapshots while preserving first-seen order.

    QQ virtualizes chat rows: a new row can push an earlier row out of the UIA
    tree between two reads. Keeping the union avoids losing either edge.
    """
    merged: list[HistoryRecord] = []
    for snapshot in snapshots:
        current = list(snapshot)
        if not merged:
            merged = current
            continue
        overlap = _history_overlap(merged, current)
        if overlap:
            merged.extend(current[overlap:])
            continue
        # A repeated or backwards/partial UI snapshot adds no new tail.
        current_tokens = [_history_token(record) for record in current]
        merged_tokens = [_history_token(record) for record in merged]
        if _is_contiguous_subsequence(current_tokens, merged_tokens):
            continue
        merged.extend(current)
    return merged


def _history_token(record: HistoryRecord) -> tuple[str, str, str]:
    # Sender and display time disappear from partially visible top rows in QQ
    # NT, so sequence matching deliberately uses only stable visible fields.
    kind = "image" if record.kind == "toast_image_notice" else "text"
    return (
        _normal(record.source_group).casefold(),
        kind,
        _normal(record.content).casefold(),
    )


def _history_overlap(
    previous: list[HistoryRecord],
    current: list[HistoryRecord],
) -> int:
    previous_tokens = [_history_token(record) for record in previous]
    current_tokens = [_history_token(record) for record in current]
    for size in range(min(len(previous_tokens), len(current_tokens)), 0, -1):
        if previous_tokens[-size:] == current_tokens[:size]:
            return size
    return 0


def _is_contiguous_subsequence(
    candidate: list[tuple[str, str, str]],
    sequence: list[tuple[str, str, str]],
) -> bool:
    if not candidate:
        return True
    size = len(candidate)
    return any(sequence[index:index + size] == candidate for index in range(len(sequence) - size + 1))


def history_delta(
    previous: list[HistoryRecord],
    current: list[HistoryRecord],
) -> list[HistoryRecord]:
    """Return only rows appended after the overlapping visible sequence."""
    if not previous:
        return current
    overlap = _history_overlap(previous, current)
    if overlap:
        return current[overlap:]
    current_tokens = [_history_token(record) for record in current]
    previous_tokens = [_history_token(record) for record in previous]
    if _is_contiguous_subsequence(current_tokens, previous_tokens):
        return []
    # With no overlap the UI likely jumped or switched context. Treat it as
    # ambiguous; notification anchoring remains the safe fallback.
    return []


def bootstrap_history_from_notifications(
    notifications: Iterable[IncomingMessage],
    visible_history: list[HistoryRecord],
) -> list[HistoryRecord]:
    """Recover the current time bucket when startup priming was unavailable."""
    matched_times: set[str] = set()
    for notification in notifications:
        identity = _sender_and_content(notification.sender, notification.content)
        matching_indexes = [
            index
            for index, record in enumerate(visible_history)
            if _sender_and_content(record.sender, record.content) == identity
        ]
        if not matching_indexes:
            continue
        display_time = visible_history[matching_indexes[-1]].display_time
        if display_time:
            matched_times.add(display_time)
    if not matched_times:
        return []
    return [
        record for record in visible_history
        if record.display_time in matched_times
    ]


def merge_notifications_with_history(
    notifications: list[IncomingMessage],
    new_history: list[HistoryRecord],
    visible_history: list[HistoryRecord] | None,
) -> list[IncomingMessage]:
    """Prefer newly backfilled rows and suppress notifications they cover."""
    if visible_history is None:
        return notifications

    merged = [
        IncomingMessage.create(
            _record_key(record),
            record.source_group,
            record.content,
            sender=record.sender,
            kind=record.kind,
        )
        for record in new_history
    ]
    visible_counts = Counter(
        _sender_and_content(record.sender, record.content)
        for record in visible_history
    )
    consumed: Counter[tuple[str, str]] = Counter()
    for notification in notifications:
        identity = _sender_and_content(notification.sender, notification.content)
        if consumed[identity] < visible_counts[identity]:
            consumed[identity] += 1
            continue
        merged.append(notification)
    return merged


class QqHistoryReader:
    """Use a notification as a trigger, then backfill visible QQ chat rows."""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        self._window_reader = QqWindowImageReader(config)
        self._last_visible: dict[str, list[HistoryRecord]] = {
            name: [] for name in config.listener_names
        }
        self._primed: set[str] = set()

    @staticmethod
    def _handle(window: Any | None) -> int:
        if window is None:
            return 0
        try:
            return int(window.handle)
        except Exception:
            return 0

    @staticmethod
    def _desktop_state(qq_window: Any | None) -> tuple[int, int, bool]:
        try:
            import win32gui

            foreground = int(win32gui.GetForegroundWindow())
            qq_handle = QqHistoryReader._handle(qq_window)
            minimized = bool(qq_handle and win32gui.IsIconic(qq_handle))
            return foreground, qq_handle, minimized
        except Exception:
            return 0, QqHistoryReader._handle(qq_window), False

    @staticmethod
    def _restore_desktop_state(state: tuple[int, int, bool]) -> None:
        foreground, qq_handle, qq_was_minimized = state
        try:
            import win32con
            import win32gui

            if qq_was_minimized and qq_handle and win32gui.IsWindow(qq_handle):
                win32gui.ShowWindow(qq_handle, win32con.SW_MINIMIZE)
            if foreground and foreground != qq_handle and win32gui.IsWindow(foreground):
                win32gui.SetForegroundWindow(foreground)
        except Exception:
            # Windows can reject SetForegroundWindow depending on foreground
            # activation policy. The read already succeeded, so this is best effort.
            return

    @staticmethod
    def _control_node(control: Any, depth: int = 2) -> UiNode:
        try:
            rect = control.rectangle()
            bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        except Exception:
            bounds = (0, 0, 0, 0)
        try:
            control_type = str(control.element_info.control_type or "")
        except Exception:
            control_type = ""
        try:
            text = _normal(control.window_text())
        except Exception:
            text = ""
        children: tuple[UiNode, ...] = ()
        if depth > 0:
            try:
                children = tuple(
                    QqHistoryReader._control_node(child, depth - 1)
                    for child in control.children()
                )
            except Exception:
                children = ()
        return UiNode(control_type, text, *bounds, children)

    @staticmethod
    def _message_window(window: Any) -> Any | None:
        try:
            window_rect = window.rectangle()
            window_width = max(1, window_rect.right - window_rect.left)
            window_height = max(1, window_rect.bottom - window_rect.top)
            candidates: list[Any] = []
            for control in window.descendants(control_type="Window"):
                rect = control.rectangle()
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                if (
                    width >= window_width * 0.45
                    and height >= window_height * 0.4
                    and rect.left >= window_rect.left + window_width * 0.2
                ):
                    candidates.append(control)
            if candidates:
                return max(
                    candidates,
                    key=lambda control: (
                        (control.rectangle().right - control.rectangle().left)
                        * (control.rectangle().bottom - control.rectangle().top)
                    ),
                )
        except Exception:
            return None
        return None

    def _snapshot_records(self, window: Any, source_group: str) -> list[HistoryRecord] | None:
        message_window = self._message_window(window)
        if message_window is None:
            LOGGER.warning("聊天补读失败：未找到 QQ 消息列表 source_group=%s", source_group)
            return None
        rect = message_window.rectangle()
        nodes = [self._control_node(control) for control in message_window.children()]
        return parse_history_nodes(nodes, int(rect.left), int(rect.right), source_group)

    def _read_visible_unlocked(
        self,
        source_group: str,
        settle_seconds: float = 0.0,
    ) -> list[HistoryRecord] | None:
        qq_window = self._window_reader._qq_window()
        if qq_window is None:
            LOGGER.warning("聊天补读失败：未找到 QQ NT 主窗口 source_group=%s", source_group)
            return None
        state = self._desktop_state(qq_window)
        try:
            window = self._window_reader._open_group(source_group)
            if window is None:
                return None
            time.sleep(0.25)
            first = self._snapshot_records(window, source_group)
            if first is None:
                return None
            snapshots = [first]
            deadline = time.monotonic() + max(0.0, settle_seconds)
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                time.sleep(min(HISTORY_SETTLE_INTERVAL_SECONDS, remaining))
                snapshot = self._snapshot_records(window, source_group)
                if snapshot is not None:
                    snapshots.append(snapshot)
            return merge_history_snapshots(snapshots)
        except Exception as exc:
            LOGGER.warning(
                "聊天补读失败 source_group=%s error=%s",
                source_group,
                type(exc).__name__,
            )
            return None
        finally:
            self._restore_desktop_state(state)

    def read_visible(
        self,
        source_group: str,
        settle_seconds: float = 0.0,
    ) -> list[HistoryRecord] | None:
        with QQ_UI_LOCK:
            return self._read_visible_unlocked(source_group, settle_seconds)

    def prime(self) -> None:
        """Record visible history at startup so it is never replayed."""
        for source_group in self.config.listener_names:
            visible = self.read_visible(source_group)
            if visible is None:
                continue
            self._last_visible[source_group] = visible
            self._primed.add(source_group)
            LOGGER.info(
                "QQ 聊天补读基线已建立 source_group=%s visible_count=%d",
                source_group,
                len(visible),
            )

    def _new_history(
        self,
        source_group: str,
        notifications: list[IncomingMessage],
    ) -> tuple[list[HistoryRecord], list[HistoryRecord] | None]:
        visible = self.read_visible(source_group, HISTORY_SETTLE_SECONDS)
        if visible is None:
            return [], None
        if source_group not in self._primed:
            recovered = bootstrap_history_from_notifications(notifications, visible)
            self._last_visible[source_group] = visible
            self._primed.add(source_group)
            if recovered:
                LOGGER.warning(
                    "QQ 聊天补读延迟建立基线，已从通知时间段恢复消息 source_group=%s count=%d",
                    source_group,
                    len(recovered),
                )
                return recovered, visible
            LOGGER.warning("QQ 聊天补读延迟建立基线，本批次保留 Windows 通知 source_group=%s", source_group)
            return [], None
        previous = self._last_visible.get(source_group, [])
        new_records = history_delta(previous, visible)
        self._last_visible[source_group] = visible
        if new_records:
            LOGGER.info(
                "QQ 聊天补读发现新增消息 source_group=%s count=%d",
                source_group,
                len(new_records),
            )
        return new_records, visible

    def reconcile(self, notifications: list[IncomingMessage]) -> list[IncomingMessage]:
        grouped: dict[str, list[IncomingMessage]] = {}
        for notification in notifications:
            grouped.setdefault(notification.source_group, []).append(notification)

        merged: list[IncomingMessage] = []
        for source_group, group_notifications in grouped.items():
            new_history, visible = self._new_history(source_group, group_notifications)
            merged.extend(merge_notifications_with_history(
                group_notifications,
                new_history,
                visible,
            ))
        return merged
