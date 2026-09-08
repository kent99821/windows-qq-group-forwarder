import asyncio
from pathlib import Path

from app.config import AppConfig, DestinationConfig, RuntimeConfig, SourceConfig
from app.main import process_pending
from app.models import IncomingMessage
from app.state_store import StateStore


class AlwaysFailSender:
    async def send(self, _message: IncomingMessage) -> None:
        raise RuntimeError("发送被拒绝")


class RecordingSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[str] = []

    async def send(self, message: IncomingMessage) -> None:
        if self.fail:
            raise RuntimeError("机器人不可用")
        self.messages.append(message.content)


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


def test_multi_bot_delivery_keeps_success_and_retries_only_failed_bot(tmp_path: Path) -> None:
    config = AppConfig(
        source=SourceConfig("A 群", "QQ", 0.2, ()),
        destination=DestinationConfig("app-1", "TEST_SECRET_1", "group-1", "[一群]", "bot-1"),
        runtime=RuntimeConfig(tmp_path / "state.sqlite3", tmp_path / "forwarder.log", False, 1),
        destinations=(
            DestinationConfig("app-1", "TEST_SECRET_1", "group-1", "[一群]", "bot-1"),
            DestinationConfig("app-2", "TEST_SECRET_2", "group-2", "[二群]", "bot-2"),
        ),
    )
    store = StateStore(config.runtime.database_path)
    first = RecordingSender()
    second = RecordingSender(fail=True)
    try:
        store.enqueue(IncomingMessage.create("multi-message", "A 群", "你好"), ["bot-1", "bot-2"])
        asyncio.run(process_pending(config, store, {"bot-1": first, "bot-2": second}))

        assert first.messages == ["你好"]
        assert store.count("failed") == 1
        assert {str(row["bot_id"]): str(row["status"]) for row in store.deliveries("multi-message")} == {
            "bot-1": "sent",
            "bot-2": "failed",
        }
        second.fail = False
        assert store.retry_failed(["multi-message"]) == 1
        asyncio.run(process_pending(config, store, {"bot-1": first, "bot-2": second}))

        assert first.messages == ["你好"]
        assert second.messages == ["你好"]
        assert store.count("sent") == 1
    finally:
        store.close()
