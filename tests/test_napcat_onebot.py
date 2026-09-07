import asyncio

import websockets

from app.config import NapcatConfig, SourceConfig, ListenerSession
from app.source.napcat_onebot import NapcatOneBotSource


def test_napcat_source_authenticates_and_emits_message(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        received_headers = {}

        async def handler(connection) -> None:
            received_headers.update(connection.request.headers)
            await connection.send(
                '{"time":1788698369,"self_id":90001,"post_type":"message",'
                '"message_type":"group","message_id":77,"group_id":"10001",'
                '"group_name":"发家致富","sender":{"nickname":"小明","card":""},'
                '"message":[{"type":"text","data":{"text":"你好"}}]}'
            )
            await connection.wait_closed()

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr("app.source.napcat_onebot.read_user_environment_variable", lambda _name: "test-token")
        source = NapcatOneBotSource(
            SourceConfig("发家致富", "QQ", 0.2, (), backend="napcat", sessions=(ListenerSession("group", "10001", "发家致富"),)),
            NapcatConfig(ws_url=f"ws://127.0.0.1:{port}"),
            tmp_path / "image-cache",
        )
        queue = asyncio.Queue()
        stop_event = asyncio.Event()
        task = asyncio.create_task(source.run(queue, stop_event))
        try:
            message = (await asyncio.wait_for(queue.get(), timeout=2))[0]
            assert message.content == "你好"
            assert message.sender == "小明"
            assert message.message_key == "napcat:90001:group:10001:77"
            assert received_headers.get("Authorization", received_headers.get("authorization")) == "Bearer test-token"
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
