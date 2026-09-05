from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


@dataclass(frozen=True)
class SourceConfig:
    group_name: str
    app_name_contains: str
    poll_interval_seconds: float
    exclude_texts: tuple[str, ...]
    image_cache_paths: tuple[Path, ...] = ()
    image_cache_match_seconds: float = 20.0
    image_cache_settle_seconds: float = 0.25
    image_cache_wait_seconds: float = 5.0
    ui_image_wait_seconds: float = 8.0


@dataclass(frozen=True)
class DestinationConfig:
    app_id: str
    client_secret_env: str
    group_openid: str
    message_prefix: str


@dataclass(frozen=True)
class RuntimeConfig:
    database_path: Path
    log_path: Path
    dry_run: bool
    max_send_attempts: int


@dataclass(frozen=True)
class AppConfig:
    source: SourceConfig
    destination: DestinationConfig
    runtime: RuntimeConfig


def _required(table: dict[str, object], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"配置项 {key} 不能为空")
    return value.strip()


def _environment_name(table: dict[str, object], key: str) -> str:
    value = _required(table, key)
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
        raise ValueError(f"配置项 {key} 必须填写环境变量名，例如 QQ_BOT_CLIENT_SECRET，不要填写密钥本身")
    return value


def load_config(path: Path) -> AppConfig:
    path = path.resolve()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    source = raw.get("source")
    destination = raw.get("destination")
    runtime = raw.get("runtime")
    if not all(isinstance(item, dict) for item in (source, destination, runtime)):
        raise ValueError("config.toml 必须包含 [source]、[destination]、[runtime]")
    assert isinstance(source, dict)
    assert isinstance(destination, dict)
    assert isinstance(runtime, dict)

    interval = source.get("poll_interval_seconds", 1.0)
    attempts = runtime.get("max_send_attempts", 3)
    if not isinstance(interval, (int, float)) or interval <= 0:
        raise ValueError("poll_interval_seconds 必须是正数")
    if not isinstance(attempts, int) or attempts < 1:
        raise ValueError("max_send_attempts 必须是正整数")
    excludes = source.get("exclude_texts", [])
    if not isinstance(excludes, list) or not all(isinstance(item, str) for item in excludes):
        raise ValueError("exclude_texts 必须是字符串数组")
    image_cache_paths_raw = source.get("image_cache_paths", [])
    if not isinstance(image_cache_paths_raw, list) or not all(isinstance(item, str) for item in image_cache_paths_raw):
        raise ValueError("image_cache_paths 必须是字符串数组")
    image_cache_match_seconds = source.get("image_cache_match_seconds", 20.0)
    image_cache_settle_seconds = source.get("image_cache_settle_seconds", 0.25)
    image_cache_wait_seconds = source.get("image_cache_wait_seconds", 5.0)
    ui_image_wait_seconds = source.get("ui_image_wait_seconds", 8.0)
    if not isinstance(image_cache_match_seconds, (int, float)) or image_cache_match_seconds <= 0:
        raise ValueError("image_cache_match_seconds 必须是正数")
    if not isinstance(image_cache_settle_seconds, (int, float)) or image_cache_settle_seconds < 0:
        raise ValueError("image_cache_settle_seconds 必须是非负数")
    if not isinstance(image_cache_wait_seconds, (int, float)) or image_cache_wait_seconds < 0:
        raise ValueError("image_cache_wait_seconds 必须是非负数")
    if not isinstance(ui_image_wait_seconds, (int, float)) or ui_image_wait_seconds <= 0:
        raise ValueError("ui_image_wait_seconds 必须是正数")

    base_dir = path.parent
    database_path = Path(str(runtime.get("database_path", "data/forwarder.sqlite3")))
    log_path = Path(str(runtime.get("log_path", "data/forwarder.log")))
    if not database_path.is_absolute():
        database_path = base_dir / database_path
    if not log_path.is_absolute():
        log_path = base_dir / log_path
    image_cache_paths = []
    for item in image_cache_paths_raw:
        cache_path = Path(item).expanduser()
        if not cache_path.is_absolute():
            cache_path = base_dir / cache_path
        image_cache_paths.append(cache_path.resolve())

    return AppConfig(
        source=SourceConfig(
            group_name=_required(source, "group_name"),
            app_name_contains=str(source.get("app_name_contains", "QQ")).strip(),
            poll_interval_seconds=float(interval),
            exclude_texts=tuple(item.strip() for item in excludes if item.strip()),
            image_cache_paths=tuple(image_cache_paths),
            image_cache_match_seconds=float(image_cache_match_seconds),
            image_cache_settle_seconds=float(image_cache_settle_seconds),
            image_cache_wait_seconds=float(image_cache_wait_seconds),
            ui_image_wait_seconds=float(ui_image_wait_seconds),
        ),
        destination=DestinationConfig(
            app_id=_required(destination, "app_id"),
            client_secret_env=_environment_name(destination, "client_secret_env"),
            group_openid=_required(destination, "group_openid"),
            message_prefix=str(destination.get("message_prefix", "[A群转发]")).strip(),
        ),
        runtime=RuntimeConfig(
            database_path=database_path,
            log_path=log_path,
            dry_run=bool(runtime.get("dry_run", True)),
            max_send_attempts=attempts,
        ),
    )


def save_group_openid(path: Path, group_openid: str) -> None:
    """只更新 destination.group_openid，保留其他 TOML 配置。"""
    if not group_openid.strip():
        raise ValueError("group_openid 不能为空")
    path = path.resolve()
    content = path.read_text(encoding="utf-8")
    pattern = re.compile(r'(?m)^(group_openid\s*=\s*)"[^"]*"\s*$')
    updated, count = pattern.subn(lambda match: f'{match.group(1)}"{group_openid}"', content)
    if count != 1:
        raise ValueError("config.toml 中未找到唯一的 destination.group_openid 配置")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(updated, encoding="utf-8")
    temp.replace(path)
