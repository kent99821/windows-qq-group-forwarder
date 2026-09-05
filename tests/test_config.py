from pathlib import Path

from app.config import load_config, save_listener_names


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
