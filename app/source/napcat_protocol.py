from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable

from ..config import ListenerSession
from ..models import IncomingMessage


def _text(value: object) -> str:
    return value if isinstance(value, str) else str(value or "")


def _event_id(event: dict[str, Any]) -> str:
    value = event.get("message_id")
    if value is not None and _text(value).strip():
        return _text(value).strip()
    # OneBot normally always provides message_id. This fallback keeps malformed
    # events deterministic for the lifetime of a reconnect, without using the
    # message body as the normal de-duplication key.
    material = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
    return "fallback-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def stable_message_key(event: dict[str, Any]) -> str:
    """Return a stable key for one OneBot message event."""
    message_type = _text(event.get("message_type") or "unknown")
    source_id = event.get("group_id") if message_type == "group" else event.get("user_id")
    if source_id is None:
        source_id = event.get("sender", {}).get("user_id") if isinstance(event.get("sender"), dict) else "unknown"
    self_id = _text(event.get("self_id") or "unknown")
    return f"napcat:{self_id}:{message_type}:{_text(source_id)}:{_event_id(event)}"


def _session_for(event: dict[str, Any], sessions: Iterable[ListenerSession]) -> ListenerSession | None:
    message_type = _text(event.get("message_type")).casefold()
    if message_type not in {"group", "private"}:
        return None
    source_id = event.get("group_id") if message_type == "group" else event.get("user_id")
    source_id_text = _text(source_id).strip()
    for session in sessions:
        if session.type == message_type and session.id == source_id_text:
            return session
    return None


def _sender(event: dict[str, Any], message_type: str) -> str | None:
    sender = event.get("sender")
    if not isinstance(sender, dict):
        return None
    if message_type == "group":
        value = sender.get("card") or sender.get("nickname") or sender.get("user_id")
    else:
        value = sender.get("nickname") or sender.get("card") or sender.get("user_id")
    value = _text(value).strip()
    return value or None


def _segment_list(event: dict[str, Any]) -> list[dict[str, Any]]:
    value = event.get("message", [])
    if isinstance(value, str):
        return [{"type": "text", "data": {"text": value}}]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict) and isinstance(item.get("type"), str)]


def _segment_data(segment: dict[str, Any]) -> dict[str, Any]:
    data = segment.get("data")
    return data if isinstance(data, dict) else {}


def message_segments(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose normalized message segments for the source and test fixtures."""
    return _segment_list(event)


def image_segments(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [segment for segment in _segment_list(event) if segment.get("type", "").casefold() == "image"]


def image_resource(segment: dict[str, Any]) -> tuple[str | None, str | None]:
    data = _segment_data(segment)
    url = data.get("url") or data.get("file_url")
    file_value = data.get("file") or data.get("path")
    return (
        _text(url).strip() or None if url is not None else None,
        _text(file_value).strip() or None if file_value is not None else None,
    )


def render_segments(event: dict[str, Any]) -> str:
    """Convert common OneBot segments to safe plain text."""
    rendered: list[str] = []
    for segment in _segment_list(event):
        kind = _text(segment.get("type")).casefold()
        data = _segment_data(segment)
        if kind == "text":
            rendered.append(_text(data.get("text")))
        elif kind == "at":
            qq = _text(data.get("qq")).strip()
            rendered.append("@全体成员" if qq.casefold() in {"all", "所有人", "全体成员"} else f"@{_text(data.get('name') or qq)}")
        elif kind == "image":
            rendered.append("[图片]")
        elif kind == "face":
            rendered.append(f"[表情{_text(data.get('id')).strip()}]".rstrip("]") + "]")
        elif kind == "reply":
            rendered.append("[回复消息]")
        elif kind in {"json", "xml"}:
            rendered.append(f"[{kind.upper()}消息]")
        else:
            rendered.append(f"[{kind or '未知'}消息]")
    return "".join(rendered).strip()


def parse_message_event(
    event: dict[str, Any],
    sessions: Iterable[ListenerSession],
) -> IncomingMessage | None:
    """Parse and filter a OneBot 11 message event."""
    if event.get("post_type") != "message":
        return None
    if event.get("message_type") not in {"group", "private"}:
        return None
    if event.get("sub_type") == "message_sent":
        return None
    session = _session_for(event, sessions)
    if session is None:
        return None
    message_type = _text(event.get("message_type"))
    source_name = session.name
    if message_type == "group":
        source_name = _text(event.get("group_name") or session.name or session.id).strip()
    content = render_segments(event)
    if not content:
        content = "[空消息]"
    timestamp = event.get("time")
    try:
        observed_at = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return IncomingMessage(
        message_key=stable_message_key(event),
        source_group=source_name,
        content=content,
        sender=_sender(event, message_type),
        kind="image" if image_segments(event) else "text",
        observed_at=observed_at,
    )

