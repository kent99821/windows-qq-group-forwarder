from pathlib import Path
import subprocess

import pytest

from app.single_instance import SingleInstanceError, SingleInstanceLock
from app.web import serve_server


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
