from pathlib import Path

from app.source.qq_history_reader import HistoryRecord
from app.web import ForwarderController


def write_config(path: Path) -> None:
    path.write_text(
        '''[source]
listener_names = ["A 群"]
group_name = "A 群"
app_name_contains = "QQ"
poll_interval_seconds = 0.2
exclude_texts = []

[destination]
app_id = "app"
client_secret_env = "TEST_SECRET"
group_openid = "group"
message_prefix = "[转发]"

[runtime]
database_path = "data/state.sqlite3"
log_path = "data/forwarder.log"
dry_run = true
max_send_attempts = 2
''',
        encoding="utf-8",
    )


class FakeHistoryReader:
    def __init__(self, _config: object) -> None:
        pass

    def read_visible(self, source_group: str, settle_seconds: float = 0.0) -> list[HistoryRecord]:
        return [
            HistoryRecord(source_group, "小明", "你好", "12:30", "history_text", 1),
            HistoryRecord(source_group, "小明", "[图片]", "12:31", "toast_image_notice", 1),
        ]


def test_history_preview_and_replay_use_stable_independent_keys(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    monkeypatch.setattr("app.web.QqHistoryReader", FakeHistoryReader)
    controller = ForwarderController(config_path)

    preview = controller.preview_history("A 群")
    ids = [item["message_id"] for item in preview["items"]]
    first = controller.replay_history("A 群", ids)
    second = controller.replay_history("A 群", ids)

    assert first == {
        "queued": 2,
        "skipped": 0,
        "message": "已加入待发送 2 条，跳过重复 0 条；启动转发服务后发送",
    }
    assert second["queued"] == 0
    assert second["skipped"] == 2
