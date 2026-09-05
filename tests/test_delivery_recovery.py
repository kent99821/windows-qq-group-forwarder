import asyncio
from pathlib import Path

from app.config import AppConfig, DestinationConfig, RuntimeConfig, SourceConfig
from app.main import process_pending
from app.models import IncomingMessage
from app.state_store import StateStore


class AlwaysFailSender:
    async def send(self, _message: IncomingMessage) -> None:
        raise RuntimeError("发送被拒绝")


def test_message_moves_to_failed_after_max_attempts(tmp_path: Path) -> None:
    config = AppConfig(
        source=SourceConfig("A 群", "QQ", 0.2, ()),
        destination=DestinationConfig("app", "TEST_SECRET", "group", "[转发]"),
        runtime=RuntimeConfig(tmp_path / "state.sqlite3", tmp_path / "forwarder.log", False, 2),
    )
    store = StateStore(config.runtime.database_path)
    try:
        store.enqueue(IncomingMessage.create("message-1", "A 群", "你好"))
        asyncio.run(process_pending(config, store, AlwaysFailSender()))  # type: ignore[arg-type]

        assert store.count("pending") == 0
        assert store.count("failed") == 1
        row = store.failed()[0]
        assert row["attempts"] == 2
        assert "发送被拒绝" in row["last_error"]
    finally:
        store.close()
