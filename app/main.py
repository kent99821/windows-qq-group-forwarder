from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import logging
from pathlib import Path

from .bot_gateway import run_gateway_forever
from .config import AppConfig, load_config
from .destination.qq_bot import OfficialQqBotSender
from .models import IncomingMessage
from .single_instance import SingleInstanceError, SingleInstanceLock
from .source.windows_notification import WindowsNotificationReader
from .source.qq_image_cache import QqImageCache
from .source.qq_window_image import QqWindowImageReader
from .state_store import StateStore


def setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
    )


async def process_pending(config: AppConfig, store: StateStore, sender: OfficialQqBotSender | None) -> None:
    logger = logging.getLogger(__name__)
    for row in store.pending():
        key = str(row["message_key"])
        if config.runtime.dry_run:
            logger.info("dry-run：保留 1 条待发送消息 key=%s", key[:12])
            continue
        if sender is None:
            raise RuntimeError("真实发送模式下未创建 B 群机器人发送器")
        logger.info(
            "准备转发消息 source_group=%s kind=%s content=%s",
            row["source_group"],
            row["kind"],
            row["content"],
        )
        for attempt in range(1, config.runtime.max_send_attempts + 1):
            try:
                await sender.send(IncomingMessage(
                    message_key=key,
                    source_group=str(row["source_group"]),
                    content=str(row["content"]),
                    sender=str(row["sender"]) if row["sender"] is not None else None,
                    kind=str(row["kind"]),
                    observed_at=str(row["observed_at"]),
                    media_path=str(row["media_path"]) if row["media_path"] is not None else None,
                ))
                store.mark_attempt(key)
                store.mark_sent(key)
                media_path = row["media_path"]
                if media_path:
                    staged_path = Path(str(media_path))
                    staging_dir = config.runtime.database_path.parent / "image-cache"
                    try:
                        if staged_path.resolve().parent == staging_dir.resolve():
                            staged_path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("已发送图片但清理暂存文件失败 path=%s error=%s", staged_path, type(exc).__name__)
                logger.info("消息已发送 key=%s attempt=%d", key[:12], attempt)
                break
            except Exception as exc:
                store.mark_attempt(key, type(exc).__name__)
                logger.warning("发送失败 key=%s attempt=%d/%d error=%s", key[:12], attempt, config.runtime.max_send_attempts, type(exc).__name__)
                if attempt >= config.runtime.max_send_attempts:
                    break
                await asyncio.sleep(min(2 ** (attempt - 1), 10))


async def run(config: AppConfig, *, dry_run: bool = False) -> None:
    logger = logging.getLogger(__name__)
    if dry_run:
        config = replace(config, runtime=replace(config.runtime, dry_run=True))
    lock_path = config.runtime.database_path.parent / "forwarder.lock"
    with SingleInstanceLock(lock_path, "转发服务"):
        store = StateStore(config.runtime.database_path)
        discarded = store.discard_legacy_pending()
        if discarded:
            logging.getLogger(__name__).warning("已废弃 %d 条旧窗口扫描待发送记录，不会发送到 B 群", discarded)
        reader = WindowsNotificationReader(config.source)
        image_cache = QqImageCache(config.source)
        window_image_reader = QqWindowImageReader(config.source)
        sender: OfficialQqBotSender | None = None
        gateway_stop = asyncio.Event()
        gateway_task: asyncio.Task[None] | None = None
        last_pending_count = -1
        try:
            reader.prime()
            image_cache.prime()
            gateway_task = asyncio.create_task(run_gateway_forever(config.destination, gateway_stop))
            if not config.runtime.dry_run:
                sender = OfficialQqBotSender(config.destination)
                await sender.start()
            logger.info(
                "Windows QQ 转发器已启动 source_names=%s notification_backend=%s dry_run=%s",
                ",".join(config.source.listener_names),
                reader.backend_name,
                config.runtime.dry_run,
            )
            while True:
                try:
                    messages = reader.poll()
                    image_messages = [message for message in messages if message.kind == "toast_image_notice"]
                    staging_dir = config.runtime.database_path.parent / "image-cache"
                    captured_by_key: dict[str, Path | None] = {}
                    grouped_images: dict[str, list[IncomingMessage]] = {}
                    for message in image_messages:
                        grouped_images.setdefault(message.source_group, []).append(message)
                    for source_group, group_messages in grouped_images.items():
                        captured_images = window_image_reader.capture_many(
                            [message.message_key for message in group_messages],
                            staging_dir,
                            source_group,
                        )
                        captured_by_key.update({
                            message.message_key: path
                            for message, path in zip(group_messages, captured_images)
                        })
                    for message in messages:
                        if message.kind == "toast_image_notice":
                            staged_image = captured_by_key.get(message.message_key)
                            if staged_image is not None:
                                message = replace(message, kind="image", media_path=str(staged_image))
                                logger.info("图片消息已通过 QQ 窗口复制并加入发送队列 path=%s", staged_image)
                            else:
                                # UI 自动化失败时仍保留缓存探测作为低干扰回退。
                                cached_image = image_cache.find_for_notification()
                                if cached_image is not None:
                                    try:
                                        staged_image = image_cache.stage(cached_image, message.message_key, staging_dir)
                                    except Exception as exc:
                                        logger.warning("QQ 图片缓存匹配成功但暂存失败，将回退为占位提示 error=%s", type(exc).__name__)
                                    else:
                                        message = replace(message, kind="image", media_path=str(staged_image))
                                        logger.info("图片消息已通过 QQ 缓存取得原图并加入发送队列 path=%s", staged_image)
                                else:
                                    logger.warning("图片原图未取得，加入 [图片] 占位提示队列")
                        if store.enqueue(message):
                            logger.info(
                                "监听到新消息 source_group=%s kind=%s content=%s",
                                message.source_group,
                                message.kind,
                                message.content,
                            )
                            if message.kind == "toast_image_notice":
                                logger.warning("图片通知不含可匹配的原图，仅转发 [图片] 占位提示")
                        else:
                            logger.debug(
                                "忽略重复消息 source_group=%s kind=%s content=%s",
                                message.source_group,
                                message.kind,
                                message.content,
                            )
                    if config.runtime.dry_run:
                        pending_count = store.count("pending")
                        if pending_count != last_pending_count:
                            logger.info("dry-run：当前待发送队列 %d 条，未调用 B 群机器人", pending_count)
                            last_pending_count = pending_count
                    else:
                        await process_pending(config, store, sender)
                except RuntimeError as exc:
                    logger.warning("本轮 Windows 通知读取失败：%s", exc)
                await asyncio.sleep(config.source.poll_interval_seconds)
        finally:
            gateway_stop.set()
            if gateway_task is not None:
                try:
                    await asyncio.wait_for(gateway_task, timeout=10)
                except asyncio.TimeoutError:
                    gateway_task.cancel()
                    await asyncio.gather(gateway_task, return_exceptions=True)
            if sender is not None:
                await sender.close()
            store.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows QQ A 群到 B 群转发器")
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
