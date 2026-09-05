from pathlib import Path
import subprocess

import pytest

from app.single_instance import SingleInstanceError, SingleInstanceLock
from app.web import ForwarderController, serve_server


def test_single_instance_lock_blocks_second_holder(tmp_path: Path) -> None:
    path = tmp_path / "service.lock"
    first = SingleInstanceLock(path, "测试服务")
    second = SingleInstanceLock(path, "测试服务")
    first.acquire()
    try:
        with pytest.raises(SingleInstanceError, match="已有实例"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_controller_detects_forwarder_started_elsewhere(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[source]\n'''
        f'''group_name = "A 群"\n'''
        f'''app_name_contains = "QQ"\n'''
        f'''poll_interval_seconds = 1.0\n'''
        f'''exclude_texts = []\n\n'''
        f'''[destination]\n'''
        f'''app_id = "app"\n'''
        f'''client_secret_env = "QQ_BOT_CLIENT_SECRET"\n'''
        f'''group_openid = "group"\n'''
        f'''message_prefix = "[转发]"\n\n'''
        f'''[runtime]\n'''
        f'''database_path = "{data_dir.as_posix()}/state.sqlite3"\n'''
        f'''log_path = "{data_dir.as_posix()}/forwarder.log"\n'''
        f'''dry_run = true\n'''
        f'''max_send_attempts = 1\n''',
        encoding="utf-8",
    )
    controller = ForwarderController(config_path)
    lock = SingleInstanceLock(data_dir / "forwarder.lock", "转发服务")
    lock.acquire()
    try:
        status = controller.status()
        assert status["running"] is True
        assert status["external_instance"] is True
        with pytest.raises(RuntimeError, match="已有转发服务"):
            controller.start(dry_run=True)
    finally:
        lock.release()

    assert controller.add_listener_group("二群")["listener_groups"] == ["A 群", "二群"]
    assert controller.remove_listener_group("二群")["listener_groups"] == ["A 群"]
    with pytest.raises(ValueError, match="至少需要保留一个监听会话"):
        controller.remove_listener_group("A 群")


def test_controller_reports_missing_secret_before_starting_real_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[source]\n'''
        f'''group_name = "联系人"\n'''
        f'''app_name_contains = "QQ"\n'''
        f'''poll_interval_seconds = 1.0\n'''
        f'''exclude_texts = []\n\n'''
        f'''[destination]\n'''
        f'''app_id = "app"\n'''
        f'''client_secret_env = "TEST_QQ_SECRET"\n'''
        f'''group_openid = "group"\n'''
        f'''message_prefix = "[转发]"\n\n'''
        f'''[runtime]\n'''
        f'''database_path = "{data_dir.as_posix()}/state.sqlite3"\n'''
        f'''log_path = "{data_dir.as_posix()}/forwarder.log"\n'''
        f'''dry_run = false\n'''
        f'''max_send_attempts = 1\n''',
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_QQ_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="Web 控制面进程未读取环境变量 TEST_QQ_SECRET"):
        ForwarderController(config_path).start()


class FakeController:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class InterruptingServer:
    def __init__(self) -> None:
        self.closed = False

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


class FailingServer(InterruptingServer):
    def serve_forever(self) -> None:
        raise RuntimeError("server failed")


@pytest.mark.parametrize("server", [InterruptingServer(), FailingServer()])
def test_serve_server_always_stops_child(server: InterruptingServer) -> None:
    controller = FakeController()
    if isinstance(server, FailingServer):
        with pytest.raises(RuntimeError, match="server failed"):
            serve_server(controller, server)  # type: ignore[arg-type]
    else:
        serve_server(controller, server)  # type: ignore[arg-type]
    assert controller.stopped is True
    assert server.closed is True
