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
