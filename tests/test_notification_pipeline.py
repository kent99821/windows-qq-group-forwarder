import asyncio
from collections import deque
from pathlib import Path
import threading

from app.config import SourceConfig
from app.main import collect_notifications, route_notification_batches
from app.models import IncomingMessage
from app.source.windows_notification import _UserNotificationListenerBackend
from app.state_store import StateStore


class FakeReader:
    def __init__(self) -> None:
        self.batches = deque([
            [IncomingMessage.create("first", "家欣", "你好")],
            [IncomingMessage.create("second", "家欣", "在吗")],
        ])

    def poll(self) -> list[IncomingMessage]:
        return self.batches.popleft() if self.batches else []

    def wait_for_change(self, timeout: float) -> None:
        return None


def test_notification_collector_keeps_polling_independently() -> None:
    async def scenario() -> None:
        reader = FakeReader()
        queue: asyncio.Queue[list[IncomingMessage]] = asyncio.Queue()
        task = asyncio.create_task(collect_notifications(reader, queue, 0.2))  # type: ignore[arg-type]
        try:
            first = await asyncio.wait_for(queue.get(), timeout=1)
            second = await asyncio.wait_for(queue.get(), timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert [message.content for message in first] == ["你好"]
        assert [message.content for message in second] == ["在吗"]

    asyncio.run(scenario())


def test_winrt_scan_drains_event_candidates_before_snapshot() -> None:
    candidate_one = ("event:1", "QQ", "家欣", "你好", "UserNotification")
    candidate_two = ("event:2", "QQ", "家欣", "在吗", "UserNotification")
    backend = object.__new__(_UserNotificationListenerBackend)
    backend.config = SourceConfig("家欣", "QQ", 0.2, ())
    backend._event_lock = threading.Lock()
    backend._event_candidates = deque([candidate_one, candidate_two])

    assert backend.scan() == [candidate_one, candidate_two]
    assert not backend._event_candidates


def test_router_enqueues_every_text_message_in_one_backfill_batch(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        store = StateStore(tmp_path / "state.sqlite3")
        input_queue: asyncio.Queue[list[IncomingMessage]] = asyncio.Queue()
        image_queue: asyncio.Queue[list[IncomingMessage]] = asyncio.Queue()
        send_signal = asyncio.Event()
        messages = [
            IncomingMessage.create(f"message-{index}", "发家致富", str(index), kind="history_text")
            for index in range(1, 7)
        ]
        task = asyncio.create_task(route_notification_batches(
            input_queue,
            image_queue,
            store,
            send_signal,
        ))
        try:
            await input_queue.put(messages)
            await asyncio.wait_for(input_queue.join(), timeout=1)
            return [str(row["content"]) for row in store.pending(limit=20)]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            store.close()

    assert asyncio.run(scenario()) == ["1", "2", "3", "4", "5", "6"]
