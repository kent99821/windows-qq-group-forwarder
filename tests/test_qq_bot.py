from datetime import datetime, timezone

from app.destination.qq_bot import format_forward_content
from app.models import IncomingMessage


def test_forward_content_includes_local_display_time() -> None:
    message = IncomingMessage(
        message_key="key",
        source_group="A 群",
        content="你好",
        sender="小明",
        observed_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).isoformat(),
    )

    result = format_forward_content("[A群转发]", message)

    local_time = datetime.fromisoformat(message.observed_at).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert result == f"[A群转发] [{local_time}] 小明: 你好"


def test_image_placeholder_includes_time_and_suffix() -> None:
    message = IncomingMessage(
        message_key="key",
        source_group="A 群",
        content="小明：[图片]",
        kind="toast_image_notice",
        observed_at="invalid",
    )

    result = format_forward_content("[A群转发]", message)

    assert result.startswith("[A群转发] [")
    assert "] 小明：[图片]（Windows 通知仅提供图片占位符，无法取得原图）" in result
