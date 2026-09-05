from pathlib import Path

import app.preflight as preflight


def write_config(path: Path, *, dry_run: bool) -> None:
    path.write_text(
        f'''[source]
listener_names = ["A 群"]
group_name = "A 群"
app_name_contains = "QQ"
poll_interval_seconds = 0.2
exclude_texts = []

[destination]
app_id = "1900000000"
client_secret_env = "PREFLIGHT_TEST_SECRET"
group_openid = "C1234567890ABCDEF"
message_prefix = "[转发]"

[runtime]
database_path = "data/state.sqlite3"
log_path = "data/forwarder.log"
dry_run = {'true' if dry_run else 'false'}
max_send_attempts = 2
''',
        encoding="utf-8",
    )


class FakeWindowReader:
    def __init__(self, _config: object) -> None:
        pass

    def _qq_window(self) -> object:
        return object()


class FakeNotificationReader:
    backend_name = "windows-user-notification-listener"

    def __init__(self, _config: object) -> None:
        pass

    def close(self) -> None:
        pass


class FakeImageCache:
    def __init__(self, _config: object) -> None:
        pass

    def inspect(self) -> dict[str, object]:
        return {"roots": ["cache"]}


def install_runtime_fakes(monkeypatch: object) -> None:
    monkeypatch.setattr(preflight, "QqWindowImageReader", FakeWindowReader)
    monkeypatch.setattr(preflight, "WindowsNotificationReader", FakeNotificationReader)
    monkeypatch.setattr(preflight, "QqImageCache", FakeImageCache)
    monkeypatch.setattr(preflight.importlib, "import_module", lambda _name: object())


def test_preflight_separates_missing_items(tmp_path: Path, monkeypatch: object) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, dry_run=False)
    install_runtime_fakes(monkeypatch)
    monkeypatch.delenv("PREFLIGHT_TEST_SECRET", raising=False)

    result = preflight.run_preflight(config_path, verify_remote=False)

    assert result["ready"] is False
    assert any(item["key"] == "client_secret" for item in result["missing"])
    assert any(item["key"] == "qq_window" for item in result["passed"])


def test_preflight_allows_missing_secret_in_dry_run(tmp_path: Path, monkeypatch: object) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, dry_run=True)
    install_runtime_fakes(monkeypatch)
    monkeypatch.delenv("PREFLIGHT_TEST_SECRET", raising=False)

    result = preflight.run_preflight(config_path, verify_remote=False)

    assert result["ready"] is True
    assert any(item["key"] == "client_secret" for item in result["warnings"])
