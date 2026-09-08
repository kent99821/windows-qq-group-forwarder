from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import logging
from pathlib import Path
import re
import unicodedata

from .bot_gateway import run_gateways_forever
from .config import AppConfig, load_config
from .destination.qq_bot import OfficialQqBotSender
from .models import IncomingMessage
from .single_instance import SingleInstanceError, SingleInstanceLock
from .source.windows_notification import WindowsNotificationReader
from .source.napcat_onebot import NapcatOneBotSource
from .source.qq_history_reader import QqHistoryReader
from .source.qq_image_cache import QqImageCache
from .source.qq_window_image import QqWindowImageReader
from .state_store import StateStore


ALL_MEMBERS_ONLY_PATTERN = re.compile(
    r"^(?:\[[^\]\r\n]{1,30}\]\s*)?(?:[^:\r\n]{1,80}\s*:\s*)?@\s*(?:所有人|全体成员)\s*$"
)


def is_low_value_message(message: IncomingMessage) -> bool:
    """Return whether a message only contains QQ's all-members mention."""
    content = unicodedata.normalize("NFKC", message.content or "")
    content = " ".join(content.split()).strip()
    return bool(ALL_MEMBERS_ONLY_PATTERN.fullmatch(content))


def setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )


async def process_pending(
    config: AppConfig,
    store: StateStore,
    sender: OfficialQqBotSender | dict[str, OfficialQqBotSender] | None,
) -> None:
    logger = logging.getLogger(__name__)
    destinations = config.destinations
    senders = sender if isinstance(sender, dict) else ({config.destination.bot_id: sender} if sender else {})
    store.ensure_deliveries(tuple(destination.bot_id for destination in destinations))
    for row in store.pending():
        key = str(row["message_key"])
        if config.runtime.dry_run:
            logger.info("dry-run：保留 %d 条机器人投递 key=%s", len(store.pending_deliveries(key)), key[:12])
            continue
        logger.info(
            "准备转发消息 source_group=%s kind=%s content=%s destinations=%d",
            row["source_group"],
            row["kind"],
            row["content"],
            len(store.pending_deliveries(key)),
        )
        message = IncomingMessage(
            message_key=key,
            source_group=str(row["source_group"]),
            content=str(row["content"]),
            sender=str(row["sender"]) if row["sender"] is not None else None,
            kind=str(row["kind"]),
            observed_at=str(row["observed_at"]),
            media_path=str(row["media_path"]) if row["media_path"] is not None else None,
        )
        for delivery in store.pending_deliveries(key):
            bot_id = str(delivery["bot_id"])
            target_sender = senders.get(bot_id)
            attempts_before = int(delivery["attempts"])
            remaining_attempts = config.runtime.max_send_attempts - attempts_before
            if target_sender is None:
                store.mark_delivery_failed(key, bot_id, "真实发送模式下未创建该机器人的发送器")
                logger.error("机器人投递失败 key=%s bot_id=%s 原因=发送器未创建", key[:12], bot_id)
                continue
            if remaining_attempts <= 0:
                store.mark_delivery_failed(key, bot_id, str(delivery["last_error"] or "已达到最大重试次数"))
                logger.error("机器人投递进入失败队列 key=%s bot_id=%s attempts=%d", key[:12], bot_id, attempts_before)
                continue
            for offset in range(remaining_attempts):
                attempt = attempts_before + offset + 1
                try:
                    await target_sender.send(message)
                    store.mark_delivery_attempt(key, bot_id)
                    store.mark_delivery_sent(key, bot_id)
                    logger.info("消息已发送 key=%s bot_id=%s attempt=%d", key[:12], bot_id, attempt)
                    break
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    store.mark_delivery_attempt(key, bot_id, error)
                    logger.warning(
                        "发送失败 key=%s bot_id=%s attempt=%d/%d error=%s",
                        key[:12], bot_id, attempt, config.runtime.max_send_attempts, type(exc).__name__,
                    )
                    if attempt >= config.runtime.max_send_attempts:
                        store.mark_delivery_failed(key, bot_id, error)
                        logger.error("机器人投递进入失败队列 key=%s bot_id=%s attempts=%d", key[:12], bot_id, attempt)
                        break
                    await asyncio.sleep(min(2 ** offset, 10))
        if store.message_fully_sent(key):
            media_path = row["media_path"]
            if media_path:
                staged_path = Path(str(media_path))
                staging_dir = config.runtime.database_path.parent / "image-cache"
                try:
                    if staged_path.resolve().parent == staging_dir.resolve():
                        staged_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("已发送图片但清理暂存文件失败 path=%s error=%s", staged_path, type(exc).__name__)


async def collect_notifications(
    reader: WindowsNotificationReader,
    output: asyncio.Queue[list[IncomingMessage]],
    poll_interval_seconds: float,
) -> None:
    """持续采集通知；发送和图片处理变慢时也不会暂停读取。"""
    logger = logging.getLogger(__name__)
    while True:
        try:
            # WinRT 对象可能绑定创建它的 COM 线程；读取在该线程执行，
            # 避免把 UserNotificationListener 跨线程传入线程池。
            messages = reader.poll()
            if messages:
                await output.put(messages)
        except RuntimeError as exc:
            logger.warning("本轮 Windows 通知读取失败：%s", exc)
        await asyncio.to_thread(reader.wait_for_change, poll_interval_seconds)


async def collect_napcat(
    source: NapcatOneBotSource,
    output: asyncio.Queue[list[IncomingMessage]],
    stop_event: asyncio.Event,
) -> None:
    """Run the event-driven NapCat source until the service is stopped."""
    await source.run(output, stop_event)


def enqueue_message(
    store: StateStore,
    message: IncomingMessage,
    destination_ids: tuple[str, ...] | list[str] = (),
) -> bool:
    logger = logging.getLogger(__name__)
    if is_low_value_message(message):
        logger.info(
            "过滤无价值的全体成员提醒 source_group=%s content=%s",
            message.source_group,
            message.content,
        )
        return False
    if store.enqueue(message, destination_ids):
        logger.info(
            "监听到新消息 source_group=%s kind=%s content=%s",
            message.source_group,
            message.kind,
            message.content,
        )
        if message.kind == "toast_image_notice":
            logger.warning("图片通知不含可匹配的原图，仅转发 [图片] 占位提示")
        return True
    logger.debug(
        "忽略重复消息 source_group=%s kind=%s content=%s",
        message.source_group,
        message.kind,
        message.content,
    )
    return False


async def route_notification_batches(
    input_queue: asyncio.Queue[list[IncomingMessage]],
    image_queue: asyncio.Queue[list[IncomingMessage]],
    store: StateStore,
    send_signal: asyncio.Event,
    history_reader: QqHistoryReader | None = None,
    destination_ids: tuple[str, ...] | list[str] = (),
) -> None:
    """补读聊天记录后路由；通知采集任务仍独立高速运行。"""
    while True:
        messages = await input_queue.get()
        try:
            if history_reader is not None:
                messages = await asyncio.to_thread(history_reader.reconcile, messages)
            image_messages = [message for message in messages if message.kind == "toast_image_notice"]
            text_messages = [message for message in messages if message.kind != "toast_image_notice"]
            enqueued = False
            for message in text_messages:
                if message.kind == "image" and not message.media_path:
                    if store.enqueue_failed(message, "NapCat 图片原图未取得，禁止发送占位消息", destination_ids):
                        logging.getLogger(__name__).error(
                            "NapCat 图片消息已记录为失败，不会发送占位提示 key=%s source_group=%s",
                            message.message_key[:12], message.source_group,
                        )
                    continue
                enqueued = enqueue_message(store, message, destination_ids) or enqueued
            if enqueued:
                send_signal.set()
            if image_messages:
                await image_queue.put(image_messages)
        finally:
            input_queue.task_done()


async def process_image_batches(
    config: AppConfig,
    image_queue: asyncio.Queue[list[IncomingMessage]],
    image_cache: QqImageCache,
    window_image_reader: QqWindowImageReader,
    store: StateStore,
    send_signal: asyncio.Event,
) -> None:
    logger = logging.getLogger(__name__)
    staging_dir = config.runtime.database_path.parent / "image-cache"
    destination_ids = tuple(destination.bot_id for destination in config.destinations)
    while True:
        messages = await image_queue.get()
        try:
            captured_by_key: dict[str, Path | None] = {}
            grouped_images: dict[str, list[IncomingMessage]] = {}
            for message in messages:
                grouped_images.setdefault(message.source_group, []).append(message)
            for source_group, group_messages in grouped_images.items():
                captured_images = await asyncio.to_thread(
                    window_image_reader.capture_many,
                    [message.message_key for message in group_messages],
                    staging_dir,
                    source_group,
                )
                captured_by_key.update({
                    message.message_key: path
                    for message, path in zip(group_messages, captured_images)
                })

            enqueued = False
            for message in messages:
                staged_image = captured_by_key.get(message.message_key)
                if staged_image is not None:
                    message = replace(message, kind="image", media_path=str(staged_image))
                    logger.info("图片消息已通过 QQ 窗口复制并加入发送队列 path=%s", staged_image)
                else:
                    cached_image = await asyncio.to_thread(image_cache.find_for_notification)
                    if cached_image is not None:
                        try:
                            staged_image = await asyncio.to_thread(
                                image_cache.stage,
                                cached_image,
                                message.message_key,
                                staging_dir,
                            )
                        except Exception as exc:
                            logger.warning(
                                "QQ 图片缓存匹配成功但暂存失败，将回退为占位提示 error=%s",
                                type(exc).__name__,
                            )
                        else:
                            message = replace(message, kind="image", media_path=str(staged_image))
                            logger.info("图片消息已通过 QQ 缓存取得原图并加入发送队列 path=%s", staged_image)
                    else:
                        logger.warning("图片原图未取得，禁止发送 [图片] 占位提示")
                if message.kind == "toast_image_notice" or not message.media_path:
                    error = "图片原图未取得：QQ 聊天窗口复制和缓存目录匹配均失败"
                    if store.enqueue_failed(message, error, destination_ids):
                        logger.error(
                            "图片消息已记录为失败，不会转发占位提示 key=%s source_group=%s content=%s",
                            message.message_key[:12],
                            message.source_group,
                            message.content,
                        )
                    else:
                        logger.debug("忽略重复的失败图片消息 key=%s", message.message_key[:12])
                    continue
                enqueued = enqueue_message(store, message, destination_ids) or enqueued
            if enqueued:
                send_signal.set()
        finally:
            image_queue.task_done()


async def send_pending_forever(
    config: AppConfig,
    store: StateStore,
    sender: dict[str, OfficialQqBotSender] | OfficialQqBotSender | None,
    send_signal: asyncio.Event,
) -> None:
    logger = logging.getLogger(__name__)
    last_pending_count = -1
    while True:
        try:
            await asyncio.wait_for(send_signal.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        send_signal.clear()
        if config.runtime.dry_run:
            pending_count = store.count("pending")
            if pending_count != last_pending_count:
                logger.info("dry-run：当前待发送队列 %d 条，未调用 QQ 群机器人", pending_count)
                last_pending_count = pending_count
        else:
            await process_pending(config, store, sender)


async def run(config: AppConfig, *, dry_run: bool = False) -> None:
    logger = logging.getLogger(__name__)
    if dry_run:
        config = replace(config, runtime=replace(config.runtime, dry_run=True))
    lock_path = config.runtime.database_path.parent / "forwarder.lock"
    with SingleInstanceLock(lock_path, "转发服务"):
        store = StateStore(config.runtime.database_path)
        # NapCat 的普通消息 kind 同样是 text，不能把它们误判成旧 UIA 队列。
        discarded = store.discard_legacy_pending() if config.source.backend == "windows_notification" else 0
        if discarded:
            logging.getLogger(__name__).warning("已废弃 %d 条旧窗口扫描待发送记录，不会发送到 QQ 群", discarded)
        napcat_source: NapcatOneBotSource | None = None
        reader: WindowsNotificationReader | None = None
        history_reader: QqHistoryReader | None = None
        image_cache: QqImageCache | None = None
        window_image_reader: QqWindowImageReader | None = None
        if config.source.backend == "napcat":
            napcat_source = NapcatOneBotSource(
                config.source,
                config.napcat,
                config.runtime.database_path.parent / "image-cache",
            )
        else:
            reader = WindowsNotificationReader(config.source)
            history_reader = QqHistoryReader(config.source)
            image_cache = QqImageCache(config.source)
            window_image_reader = QqWindowImageReader(config.source)
        senders: dict[str, OfficialQqBotSender] = {}
        destination_ids = tuple(destination.bot_id for destination in config.destinations)
        store.ensure_deliveries(destination_ids)
        gateway_stop = asyncio.Event()
        gateway_task: asyncio.Task[None] | None = None
        notification_queue: asyncio.Queue[list[IncomingMessage]] = asyncio.Queue()
        image_queue: asyncio.Queue[list[IncomingMessage]] = asyncio.Queue()
        send_signal = asyncio.Event()
        worker_tasks: list[asyncio.Task[None]] = []
        try:
            if reader is not None and image_cache is not None:
                reader.prime()
                image_cache.prime()
            gateway_task = asyncio.create_task(run_gateways_forever(config.destinations, gateway_stop))
            if not config.runtime.dry_run:
                for destination in config.destinations:
                    current_sender = OfficialQqBotSender(destination)
                    await current_sender.start()
                    senders[destination.bot_id] = current_sender
            if history_reader is not None:
                await asyncio.to_thread(history_reader.prime)
            logger.info(
                "QQ 转发器已启动 source_backend=%s source_names=%s notification_backend=%s destinations=%d dry_run=%s",
                config.source.backend,
                ",".join(config.source.listener_names),
                reader.backend_name if reader is not None else "napcat-onebot-websocket",
                len(config.destinations),
                config.runtime.dry_run,
            )
            send_signal.set()
            source_stop = asyncio.Event()
            source_task = (
                asyncio.create_task(
                    collect_napcat(napcat_source, notification_queue, source_stop),
                    name="napcat-onebot-source",
                )
                if napcat_source is not None
                else asyncio.create_task(
                    collect_notifications(reader, notification_queue, config.source.poll_interval_seconds),  # type: ignore[arg-type]
                    name="qq-notification-collector",
                )
            )
            worker_tasks = [source_task,
                asyncio.create_task(
                    route_notification_batches(
                        notification_queue,
                        image_queue,
                        store,
                        send_signal,
                        history_reader,
                        destination_ids,
                    ),
                    name="qq-notification-router",
                ),
                asyncio.create_task(
                    send_pending_forever(config, store, senders, send_signal),
                    name="qq-message-sender",
                ),
            ]
            if reader is not None and image_cache is not None and window_image_reader is not None:
                worker_tasks.insert(
                    2,
                    asyncio.create_task(
                        process_image_batches(
                            config,
                            image_queue,
                            image_cache,
                            window_image_reader,
                            store,
                            send_signal,
                        ),
                        name="qq-image-processor",
                    ),
                )
            await asyncio.gather(*worker_tasks)
        finally:
            if 'source_stop' in locals():
                source_stop.set()
            for task in worker_tasks:
                task.cancel()
            if worker_tasks:
                await asyncio.gather(*worker_tasks, return_exceptions=True)
            if reader is not None:
                reader.close()
            if napcat_source is not None:
                await napcat_source.close()
            gateway_stop.set()
            if gateway_task is not None:
                try:
                    await asyncio.wait_for(gateway_task, timeout=10)
                except asyncio.TimeoutError:
                    gateway_task.cancel()
                    await asyncio.gather(gateway_task, return_exceptions=True)
            for current_sender in senders.values():
                await current_sender.close()
            store.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows QQ A 群到 QQ 群转发器")
    parser.add_argument("command", choices=["run", "inspect-window", "inspect-image-cache", "web"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--dry-run", action="store_true", help="只读取和入队，不真实发送")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if args.command == "inspect-window":
        import json
        reader = WindowsNotificationReader(config.source)
        print(json.dumps(reader.inspect(), ensure_ascii=False, indent=2))
        return
    if args.command == "inspect-image-cache":
        import json
        image_cache = QqImageCache(config.source)
        print(json.dumps(image_cache.inspect(), ensure_ascii=False, indent=2))
        return
    setup_logging(config.runtime.log_path)
    if args.command == "web":
        from .web import create_server, ForwarderController, serve_server
        lock_path = config.runtime.database_path.parent / "web.lock"
        with SingleInstanceLock(lock_path, "Web 控制面"):
            controller = ForwarderController(args.config)
            server = create_server(controller, args.host, args.port, Path(__file__).resolve().parent.parent / "web")
            print(f"Web 控制面已启动：http://{args.host}:{args.port}")
            serve_server(controller, server)
        return
    try:
        asyncio.run(run(config, dry_run=args.dry_run))
    except KeyboardInterrupt:
        print("已停止。")


if __name__ == "__main__":
    try:
        main()
    except SingleInstanceError as exc:
        raise SystemExit(str(exc)) from None
