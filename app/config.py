from __future__ import annotations

from dataclasses import dataclass
import json
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
    image_cache_match_seconds: float = 60.0
    image_cache_settle_seconds: float = 0.25
    image_cache_wait_seconds: float = 45.0
    ui_image_wait_seconds: float = 8.0
    group_names: tuple[str, ...] = ()
    listener_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # listener_names 是通用配置名；group_names/group_name 保留用于兼容旧配置。
        configured_names = self.listener_names or self.group_names
        names = tuple(dict.fromkeys(
            name.strip() for name in configured_names if isinstance(name, str) and name.strip()
        ))
        if not names and self.group_name.strip():
            names = (self.group_name.strip(),)
        if not names:
            raise ValueError("至少需要配置一个监听会话名称")
        object.__setattr__(self, "listener_names", names)
        object.__setattr__(self, "group_names", names)
        object.__setattr__(self, "group_name", names[0])


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

    interval = source.get("poll_interval_seconds", 0.2)
    attempts = runtime.get("max_send_attempts", 3)
    if not isinstance(interval, (int, float)) or interval <= 0:
        raise ValueError("poll_interval_seconds 必须是正数")
    if not isinstance(attempts, int) or attempts < 1:
        raise ValueError("max_send_attempts 必须是正整数")
    listener_names_raw = source.get("listener_names")
    legacy_group_names_raw = source.get("group_names")
    names_raw = listener_names_raw if listener_names_raw is not None else legacy_group_names_raw
    names_key = "listener_names" if listener_names_raw is not None else "group_names"
    if names_raw is None:
        listener_names = (_required(source, "group_name"),)
    else:
        if not isinstance(names_raw, list) or not all(isinstance(item, str) for item in names_raw):
            raise ValueError(f"{names_key} 必须是字符串数组")
        listener_names = tuple(dict.fromkeys(item.strip() for item in names_raw if item.strip()))
        if not listener_names:
            raise ValueError(f"{names_key} 至少需要包含一个监听会话名称")
    excludes = source.get("exclude_texts", [])
    if not isinstance(excludes, list) or not all(isinstance(item, str) for item in excludes):
        raise ValueError("exclude_texts 必须是字符串数组")
    image_cache_paths_raw = source.get("image_cache_paths", [])
    if not isinstance(image_cache_paths_raw, list) or not all(isinstance(item, str) for item in image_cache_paths_raw):
        raise ValueError("image_cache_paths 必须是字符串数组")
    image_cache_match_seconds = source.get("image_cache_match_seconds", 60.0)
    image_cache_settle_seconds = source.get("image_cache_settle_seconds", 0.25)
    image_cache_wait_seconds = source.get("image_cache_wait_seconds", 45.0)
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
            group_name=listener_names[0],
            app_name_contains=str(source.get("app_name_contains", "QQ")).strip(),
            poll_interval_seconds=float(interval),
            exclude_texts=tuple(item.strip() for item in excludes if item.strip()),
            image_cache_paths=tuple(image_cache_paths),
            image_cache_match_seconds=float(image_cache_match_seconds),
            image_cache_settle_seconds=float(image_cache_settle_seconds),
            image_cache_wait_seconds=float(image_cache_wait_seconds),
            ui_image_wait_seconds=float(ui_image_wait_seconds),
            group_names=listener_names,
            listener_names=listener_names,
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


def save_dry_run(path: Path, dry_run: bool) -> None:
    """Update runtime.dry_run while preserving the rest of config.toml."""
    path = path.resolve()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    runtime_start = next((index for index, line in enumerate(lines) if line.strip() == "[runtime]"), None)
    if runtime_start is None:
        raise ValueError("config.toml 中未找到 [runtime] 配置段")
    runtime_end = next(
        (index for index in range(runtime_start + 1, len(lines))
         if lines[index].strip().startswith("[") and lines[index].strip().endswith("]")),
        len(lines),
    )
    runtime_body = lines[runtime_start + 1:runtime_end]
    newline = "\n"
    if lines and lines[0].endswith("\r\n"):
        newline = "\r\n"
    replacement = f"dry_run = {'true' if dry_run else 'false'}{newline}"
    pattern = re.compile(r"^\s*dry_run\s*=")
    for index, line in enumerate(runtime_body):
        if pattern.match(line):
            runtime_body[index] = replacement
            break
    else:
        runtime_body.insert(0, replacement)
    updated_lines = lines[:runtime_start + 1] + runtime_body + lines[runtime_end:]
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(updated_lines), encoding="utf-8")
    temp.replace(path)


def save_listener_names(path: Path, listener_names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Update generic QQ conversation names while preserving the rest of config.toml."""
    normalized = tuple(dict.fromkeys(
        name.strip() for name in listener_names if isinstance(name, str) and name.strip()
    ))
    if not normalized:
        raise ValueError("至少需要保留一个监听会话")
    if any("\n" in name or "\r" in name or len(name) > 120 for name in normalized):
        raise ValueError("会话名称不能为空，不能包含换行，且不能超过 120 个字符")

    path = path.resolve()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    source_start = next((index for index, line in enumerate(lines) if line.strip() == "[source]"), None)
    if source_start is None:
        raise ValueError("config.toml 中未找到 [source] 配置段")
    source_end = next(
        (index for index in range(source_start + 1, len(lines)) if lines[index].strip().startswith("[") and lines[index].strip().endswith("]")),
        len(lines),
    )
    serialized = json.dumps(list(normalized), ensure_ascii=False)
    first_serialized = json.dumps(normalized[0], ensure_ascii=False)
    newline = "\n"
    if lines and lines[0].endswith("\r\n"):
        newline = "\r\n"
    source_body = lines[source_start + 1:source_end]

    def set_option(option: str, value: str) -> None:
        pattern = re.compile(rf"^\s*{re.escape(option)}\s*=")
        for index, line in enumerate(source_body):
            if pattern.match(line):
                source_body[index] = f"{option} = {value}{newline}"
                return
        source_body.insert(0, f"{option} = {value}{newline}")

    # 写入新配置名，同时更新两个旧别名，确保旧版本仍能读取。
    set_option("listener_names", serialized)
    set_option("group_names", serialized)
    set_option("group_name", first_serialized)
    lines = lines[:source_start + 1] + source_body + lines[source_end:]

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(lines), encoding="utf-8")
    temp.replace(path)
    return normalized


def save_listener_groups(path: Path, group_names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Backward-compatible alias for callers using the old group-specific name."""
    return save_listener_names(path, group_names)
