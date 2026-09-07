from pathlib import Path

from app.models import IncomingMessage
from app.state_store import StateStore


def test_enqueue_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    try:
        message = IncomingMessage.create("same-key", "A 群", "你好")
        assert store.enqueue(message) is True
        assert store.enqueue(message) is False
        assert len(store.pending()) == 1
        store.mark_attempt("same-key")
        store.mark_sent("same-key")
        assert store.count("pending") == 0
        assert store.count("sent") == 1
    finally:
        store.close()


def test_enqueue_persists_media_path(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    try:
        message = IncomingMessage.create(
            "image-key", "A 群", "家欣：[图片]", kind="image", media_path="image-cache/a.jpg"
        )
        assert store.enqueue(message) is True
        row = store.pending()[0]
        assert row["kind"] == "image"
        assert row["media_path"] == "image-cache/a.jpg"
    finally:
        store.close()


def test_failed_messages_can_be_selected_for_retry(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    try:
        first = IncomingMessage.create("failed-1", "A 群", "第一条")
        second = IncomingMessage.create("failed-2", "A 群", "第二条")
        store.enqueue(first)
        store.enqueue(second)
        store.mark_attempt("failed-1", "网络错误")
        store.mark_failed("failed-1", "网络错误")
        store.mark_attempt("failed-2", "权限错误")
        store.mark_failed("failed-2", "权限错误")

        assert store.summary()["failed"] == 2
        assert store.retry_failed(["failed-1"]) == 1
        assert [row["message_key"] for row in store.pending()] == ["failed-1"]
        assert store.failed()[0]["message_key"] == "failed-2"
        retried = store.pending()[0]
        assert retried["attempts"] == 0
        assert retried["last_error"] is None
    finally:
        store.close()


def test_message_can_be_recorded_as_failed_without_entering_pending_queue(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    try:
        message = IncomingMessage.create(
            "missing-image",
            "A 群",
            "小明：[图片]",
            kind="toast_image_notice",
        )

        assert store.enqueue_failed(message, "图片原图未取得") is True
        assert store.count("pending") == 0
        assert store.count("failed") == 1
        assert store.failed()[0]["last_error"] == "图片原图未取得"
    finally:
        store.close()
