from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import load_config
from .source.napcat_onebot import NapcatOneBotSource


async def _run(config_path: Path) -> None:
    config = load_config(config_path)
    if config.source.backend != "napcat":
        raise RuntimeError("POC 要求 source.backend = \"napcat\"，不会启动 Windows 通知监听")
    source = NapcatOneBotSource(
        config.source,
        config.napcat,
        config.runtime.database_path.parent / "image-cache",
    )
    queue: asyncio.Queue = asyncio.Queue()
    stop_event = asyncio.Event()
    task = asyncio.create_task(source.run(queue, stop_event), name="napcat-poc-source")
    print("NapCat 只读 POC 已启动；按 Ctrl+C 停止。不会调用 QQ 官方机器人发送接口。")
    try:
        while True:
            batch = await queue.get()
            for message in batch:
                print(json.dumps({
                    "message_key": message.message_key,
                    "source_group": message.source_group,
                    "sender": message.sender,
                    "kind": message.kind,
                    "content": message.content,
                    "observed_at": message.observed_at,
                    "media_path": message.media_path,
                }, ensure_ascii=False))
            queue.task_done()
    finally:
        stop_event.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await source.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="NapCat OneBot 11 只读消息 POC")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.config))
    except KeyboardInterrupt:
        print("NapCat 只读 POC 已停止。")


if __name__ == "__main__":
    main()
