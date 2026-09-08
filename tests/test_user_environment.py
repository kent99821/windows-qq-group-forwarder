import asyncio
import sys
import types

from app import environment
from app import bot_gateway
from app.config import DestinationConfig


def _install_fake_user_registry(monkeypatch: object, value: str) -> None:
    class FakeKey:
        def __enter__(self) -> "FakeKey":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        OpenKey=lambda root, path: FakeKey(),
        QueryValueEx=lambda key, name: (value, 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)


def test_read_user_environment_variable_prefers_process_value(monkeypatch: object) -> None:
    _install_fake_user_registry(monkeypatch, "user-secret")
    monkeypatch.setattr(environment.os, "name", "nt")
    monkeypatch.setenv("QQ_BOT_CLIENT_SECRET2", "process-secret")

    assert environment.read_user_environment_variable("QQ_BOT_CLIENT_SECRET2") == "process-secret"


def test_read_user_environment_variable_falls_back_to_hkcu(monkeypatch: object) -> None:
    _install_fake_user_registry(monkeypatch, "user-secret")
    monkeypatch.setattr(environment.os, "name", "nt")
    monkeypatch.delenv("QQ_BOT_CLIENT_SECRET2", raising=False)

    assert environment.read_user_environment_variable("QQ_BOT_CLIENT_SECRET2") == "user-secret"


def test_bind_group_uses_user_environment_variable(monkeypatch: object) -> None:
    destination = DestinationConfig("app-2", "USER_SECRET_2", "待绑定", "[转发]", "bot-2")
    monkeypatch.delenv("USER_SECRET_2", raising=False)
    monkeypatch.setattr(bot_gateway, "read_user_environment_variable", lambda _name: "user-secret")

    class FakeApi:
        def __init__(self, app_id: str, client_secret: str) -> None:
            assert app_id == "app-2"
            assert client_secret == "user-secret"

        def setup(self, _client: object) -> None:
            pass

        async def ensure_token(self) -> None:
            pass

        def ensure_token_sync(self) -> str:
            return "token"

        async def send_text(self, *_args: object, **_kwargs: object) -> dict[str, str]:
            return {"id": "reply-1"}

        def clear_token(self) -> None:
            pass

        def get_gateway_url_sync(self) -> str:
            return "ws://example.test"

    class FakeParser:
        def parse(self, _event_type: str, _raw: dict[str, object]) -> object:
            return types.SimpleNamespace(
                chat_scope="group",
                chat_id="group-2",
                content="绑定",
                message_id="message-1",
            )

    class FakeWebSocket:
        def __init__(self, callbacks: object, **_kwargs: object) -> None:
            self.callbacks = callbacks

        def start(self, _url: str, loop: object) -> None:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self.callbacks.on_message_event("GROUP_MESSAGE", {})
                )
            )

        async def async_stop(self) -> None:
            pass

    fake_sdk = types.SimpleNamespace(
        EventParser=FakeParser,
        QQApiClient=FakeApi,
        QQWebSocket=FakeWebSocket,
        WSCallbacks=lambda **kwargs: types.SimpleNamespace(**kwargs),
    )
    monkeypatch.setitem(sys.modules, "qqbot_agent_sdk", fake_sdk)

    assert asyncio.run(bot_gateway.bind_group(destination, timeout_seconds=1)) == "group-2"
