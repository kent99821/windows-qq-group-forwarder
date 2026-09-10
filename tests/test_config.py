from pathlib import Path

from app.config import (
    DestinationConfig,
    ListenerSession,
    load_config,
    save_destinations,
    save_dry_run,
    save_listener_names,
    save_listener_sessions,
    save_source_backend,
)


def write_config(path: Path) -> None:
    path.write_text(
        '''[source]
group_name = "旧群"
app_name_contains = "QQ"
poll_interval_seconds = 1.0
exclude_texts = ["旧群"]

[destination]
app_id = "app"
client_secret_env = "QQ_BOT_CLIENT_SECRET"
group_openid = "group"
message_prefix = "[转发]"

[runtime]
database_path = "data/state.sqlite3"
log_path = "data/forwarder.log"
dry_run = true
max_send_attempts = 1
''',
        encoding="utf-8",
    )


def test_save_listener_groups_updates_legacy_and_list_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)

    saved = save_listener_names(path, ["发家致富", "第二个群"])
    config = load_config(path)

    assert saved == ("发家致富", "第二个群")
    assert config.source.listener_names == ("发家致富", "第二个群")
    assert config.source.group_name == "发家致富"
    assert config.source.group_names == ("发家致富", "第二个群")
    content = path.read_text(encoding="utf-8")
    assert 'group_name = "发家致富"' in content
    assert 'group_names = ["发家致富", "第二个群"]' in content
    assert 'listener_names = ["发家致富", "第二个群"]' in content


def test_legacy_single_group_config_remains_supported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)

    config = load_config(path)

    assert config.source.group_names == ("旧群",)


def test_contact_name_is_a_valid_listener_name(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'group_name = "旧群"',
            'listener_names = ["联系人昵称"]\ngroup_name = "旧群"',
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.source.listener_names == ("联系人昵称",)
    assert config.source.group_name == "联系人昵称"


def test_save_dry_run_updates_runtime_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)

    save_dry_run(path, False)

    assert load_config(path).runtime.dry_run is False
    assert 'dry_run = false' in path.read_text(encoding="utf-8")


def test_napcat_config_loads_stable_session_ids(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '''[source]
backend = "napcat"
listener_names = ["展示名称"]
group_name = "展示名称"
app_name_contains = "QQ"
poll_interval_seconds = 0.2
exclude_texts = []

[[source.sessions]]
type = "group"
id = "10001"
name = "发家致富"

[[source.sessions]]
type = "contact"
id = "20002"
name = "家欣"

[destination]
app_id = "app"
client_secret_env = "QQ_BOT_CLIENT_SECRET"
group_openid = "group"
message_prefix = "[转发]"

[runtime]
database_path = "data/state.sqlite3"
log_path = "data/forwarder.log"
dry_run = true
max_send_attempts = 1

[napcat]
enabled = true
ws_url = "ws://127.0.0.1:3001"
token_env = "NAPCAT_ONEBOT_TOKEN"
''',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.source.backend == "napcat"
    assert [(item.type, item.id, item.name) for item in config.source.sessions] == [
        ("group", "10001", "发家致富"),
        ("private", "20002", "家欣"),
    ]
    assert config.napcat.enabled is True


def test_save_listener_sessions_replaces_existing_array_tables(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '''[source]
backend = "napcat"
listener_names = ["展示名称"]
group_name = "展示名称"
app_name_contains = "QQ"
poll_interval_seconds = 0.2
exclude_texts = []
[[source.sessions]]
type = "group"
id = "old"
name = "旧群"

[destination]
app_id = "app"
client_secret_env = "QQ_BOT_CLIENT_SECRET"
group_openid = "group"

[runtime]
database_path = "data/state.sqlite3"
log_path = "data/forwarder.log"
dry_run = true
max_send_attempts = 1
''',
        encoding="utf-8",
    )

    save_listener_sessions(path, [ListenerSession("group", "10001", "新群")])
    config = load_config(path)

    assert [(item.type, item.id, item.name) for item in config.source.sessions] == [("group", "10001", "新群")]
    assert "id = \"old\"" not in path.read_text(encoding="utf-8")


def test_napcat_config_can_use_sessions_without_legacy_name_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '''[source]
backend = "napcat"
app_name_contains = "QQ"
exclude_texts = []
[[source.sessions]]
type = "group"
id = "10001"
name = "发家致富"

[destination]
app_id = "app"
client_secret_env = "QQ_BOT_CLIENT_SECRET"
group_openid = "group"

[runtime]
database_path = "data/state.sqlite3"
log_path = "data/forwarder.log"
dry_run = true
max_send_attempts = 1
''',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.source.listener_names == ("发家致富",)


def test_multiple_destinations_load_and_save(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    destinations = [
        DestinationConfig("app-1", "SECRET_1", "group-1", "[一群]", "bot-1"),
        DestinationConfig("app-2", "SECRET_2", "group-2", "[二群]", "bot-2"),
    ]

    save_destinations(path, destinations)
    config = load_config(path)

    assert [(item.bot_id, item.app_id, item.group_openid) for item in config.destinations] == [
        ("bot-1", "app-1", "group-1"),
        ("bot-2", "app-2", "group-2"),
    ]
    assert config.destination.bot_id == "bot-1"
    assert "[destination]" not in path.read_text(encoding="utf-8")


def test_save_source_backend_persists_napcat(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)

    save_source_backend(path, "napcat")

    assert load_config(path).source.backend == "napcat"
