from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib


@dataclass(frozen=True)
class ListenerSession:
    """A stable QQ conversation identifier used by the NapCat source."""

    type: str
    id: str
    name: str = ""

    def __post_init__(self) -> None:
        session_type = self.type.strip().casefold()
        if session_type == "contact":
            session_type = "private"
        if session_type not in {"group", "private"}:
            raise ValueError("监听会话 type 必须是 group 或 private")
        if not self.id.strip():
            raise ValueError("监听会话 id 不能为空")
        object.__setattr__(self, "type", session_type)
        object.__setattr__(self, "id", self.id.strip())
        object.__setattr__(self, "name", self.name.strip() or self.id.strip())


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
    backend: str = "windows_notification"
    sessions: tuple[ListenerSession, ...] = ()

    def __post_init__(self) -> None:
        # listener_names 是通用配置名；group_names/group_name 保留用于兼容旧配置。
        configured_names = self.listener_names or self.group_names
        names = tuple(dict.fromkeys(
            name.strip() for name in configured_names if isinstance(name, str) and name.strip()
        ))
        if not names and self.group_name.strip():
            names = (self.group_name.strip(),)
        if not names and self.sessions:
            names = tuple(session.name for session in self.sessions)
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
    bot_id: str = ""

    def __post_init__(self) -> None:
        bot_id = self.bot_id.strip() or self.app_id.strip()
        if not bot_id:
            raise ValueError("机器人 bot_id 不能为空")
        if any(char in bot_id for char in "\r\n"):
            raise ValueError("机器人 bot_id 不能包含换行")
        object.__setattr__(self, "bot_id", bot_id)


@dataclass(frozen=True)
class RuntimeConfig:
    database_path: Path
    log_path: Path
    dry_run: bool
    max_send_attempts: int


@dataclass(frozen=True)
class NapcatConfig:
    enabled: bool = False
    ws_url: str = "ws://127.0.0.1:3001"
    token_env: str = "NAPCAT_ONEBOT_TOKEN"
    connect_timeout_seconds: float = 10.0
    heartbeat_timeout_seconds: float = 30.0
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    download_timeout_seconds: float = 20.0


@dataclass(frozen=True)
class AppConfig:
    source: SourceConfig
    destination: DestinationConfig
    runtime: RuntimeConfig
    napcat: NapcatConfig = NapcatConfig()
    destinations: tuple[DestinationConfig, ...] = ()

    def __post_init__(self) -> None:
        destinations = tuple(self.destinations) or (self.destination,)
        if not destinations:
            raise ValueError("至少需要配置一个转发机器人")
        seen: set[str] = set()
        for destination in destinations:
            if destination.bot_id in seen:
                raise ValueError(f"机器人 bot_id 重复：{destination.bot_id}")
            seen.add(destination.bot_id)
        object.__setattr__(self, "destinations", destinations)
        object.__setattr__(self, "destination", destinations[0])


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
    destinations_raw = raw.get("destinations")
    runtime = raw.get("runtime")
    napcat = raw.get("napcat", {})
    if not all(isinstance(item, dict) for item in (source, runtime, napcat)):
        raise ValueError("config.toml 必须包含 [source]、[runtime]，NapCat 配置可选")
    assert isinstance(source, dict)
    assert isinstance(runtime, dict)
    assert isinstance(napcat, dict)

    if destinations_raw is not None:
        if not isinstance(destinations_raw, list) or not all(isinstance(item, dict) for item in destinations_raw):
            raise ValueError("destinations 必须是对象数组")
        destination_tables = destinations_raw
    elif isinstance(destination, dict):
        destination_tables = [destination]
    else:
        raise ValueError("config.toml 必须包含 [destination] 或至少一个 [[destinations]]")
    if not destination_tables:
        raise ValueError("至少需要配置一个转发机器人")

    backend = str(source.get("backend", "windows_notification")).strip().casefold()
    if backend not in {"windows_notification", "napcat"}:
        raise ValueError("source.backend 必须是 windows_notification 或 napcat")

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
        legacy_name = source.get("group_name")
        if isinstance(legacy_name, str) and legacy_name.strip():
            listener_names = (legacy_name.strip(),)
        elif backend == "napcat":
            listener_names = ()
        else:
            listener_names = (_required(source, "group_name"),)
    else:
        if not isinstance(names_raw, list) or not all(isinstance(item, str) for item in names_raw):
            raise ValueError(f"{names_key} 必须是字符串数组")
        listener_names = tuple(dict.fromkeys(item.strip() for item in names_raw if item.strip()))
        if not listener_names and backend != "napcat":
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

    sessions_raw = source.get("sessions", [])
    if not isinstance(sessions_raw, list):
        raise ValueError("source.sessions 必须是对象数组")
    sessions: list[ListenerSession] = []
    seen_sessions: set[tuple[str, str]] = set()
    for index, item in enumerate(sessions_raw):
        if not isinstance(item, dict):
            raise ValueError(f"source.sessions[{index}] 必须是对象")
        session_type = item.get("type")
        session_id = item.get("id")
        session_name = item.get("name", "")
        if not isinstance(session_type, str) or not session_type.strip():
            raise ValueError(f"source.sessions[{index}].type 不能为空")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError(f"source.sessions[{index}].id 不能为空")
        if not isinstance(session_name, str):
            raise ValueError(f"source.sessions[{index}].name 必须是字符串")
        session = ListenerSession(session_type, session_id, session_name)
        identity = (session.type, session.id)
        if identity in seen_sessions:
            raise ValueError(f"source.sessions 中存在重复会话：{session.type}/{session.id}")
        seen_sessions.add(identity)
        sessions.append(session)

    napcat_enabled = napcat.get("enabled", backend == "napcat")
    if not isinstance(napcat_enabled, bool):
        raise ValueError("napcat.enabled 必须是布尔值")
    ws_url = str(napcat.get("ws_url", "ws://127.0.0.1:3001")).strip()
    if not ws_url.startswith(("ws://", "wss://")):
        raise ValueError("napcat.ws_url 必须以 ws:// 或 wss:// 开头")
    token_env = _environment_name(napcat, "token_env") if "token_env" in napcat else "NAPCAT_ONEBOT_TOKEN"
    napcat_numbers = {
        "connect_timeout_seconds": (10.0, False),
        "heartbeat_timeout_seconds": (30.0, False),
        "reconnect_min_seconds": (1.0, False),
        "reconnect_max_seconds": (30.0, False),
        "download_timeout_seconds": (20.0, False),
    }
    napcat_values: dict[str, float] = {}
    for key, (default, allow_zero) in napcat_numbers.items():
        value = napcat.get(key, default)
        if not isinstance(value, (int, float)) or (value < 0 if allow_zero else value <= 0):
            raise ValueError(f"napcat.{key} 必须是正数")
        napcat_values[key] = float(value)
    if napcat_values["reconnect_max_seconds"] < napcat_values["reconnect_min_seconds"]:
        raise ValueError("napcat.reconnect_max_seconds 不能小于 reconnect_min_seconds")

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

    destinations = tuple(
        DestinationConfig(
            app_id=_required(item, "app_id"),
            client_secret_env=_environment_name(item, "client_secret_env"),
            group_openid=_required(item, "group_openid"),
            message_prefix=str(item.get("message_prefix", "[A群转发]")).strip(),
            bot_id=str(item.get("bot_id", "")).strip(),
        )
        for item in destination_tables
    )

    return AppConfig(
        source=SourceConfig(
            group_name=listener_names[0] if listener_names else (sessions[0].name if sessions else ""),
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
            backend=backend,
            sessions=tuple(sessions),
        ),
        destination=destinations[0],
        runtime=RuntimeConfig(
            database_path=database_path,
            log_path=log_path,
            dry_run=bool(runtime.get("dry_run", True)),
            max_send_attempts=attempts,
        ),
        napcat=NapcatConfig(
            enabled=napcat_enabled,
            ws_url=ws_url,
            token_env=token_env,
            **napcat_values,
        ),
        destinations=destinations,
    )


def save_group_openid(path: Path, group_openid: str) -> None:
    """兼容旧调用：更新第一个转发机器人的目标群。"""
    if not group_openid.strip():
        raise ValueError("group_openid 不能为空")
    config = load_config(path)
    save_destination_group_openid(path, config.destinations[0].bot_id, group_openid)


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


def _replace_table_option(path: Path, table_name: str, option: str, value: str) -> None:
    path = path.resolve()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    table_start = next((index for index, line in enumerate(lines) if line.strip() == table_name), None)
    if table_start is None:
        lines.append(f"\n{table_name}\n")
        table_start = len(lines) - 1
    table_end = next(
        (index for index in range(table_start + 1, len(lines))
         if lines[index].strip().startswith("[") and not lines[index].strip().startswith("[[") and lines[index].strip().endswith("]")),
        len(lines),
    )
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    pattern = re.compile(rf"^\s*{re.escape(option)}\s*=")
    for index in range(table_start + 1, table_end):
        if pattern.match(lines[index]):
            lines[index] = f"{option} = {value}{newline}"
            break
    else:
        lines.insert(table_start + 1, f"{option} = {value}{newline}")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(lines), encoding="utf-8")
    temp.replace(path)


def _table_end(lines: list[str], start: int) -> int:
    return next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip().startswith("[") and lines[index].strip().endswith("]")
        ),
        len(lines),
    )


def _serialize_destinations(destinations: tuple[DestinationConfig, ...], newline: str) -> list[str]:
    body: list[str] = []
    for destination in destinations:
        body.extend([
            "[[destinations]]" + newline,
            f"bot_id = {json.dumps(destination.bot_id, ensure_ascii=False)}" + newline,
            f"app_id = {json.dumps(destination.app_id, ensure_ascii=False)}" + newline,
            f"client_secret_env = {json.dumps(destination.client_secret_env, ensure_ascii=False)}" + newline,
            f"group_openid = {json.dumps(destination.group_openid, ensure_ascii=False)}" + newline,
            f"message_prefix = {json.dumps(destination.message_prefix, ensure_ascii=False)}" + newline,
            newline,
        ])
    return body


def save_destinations(path: Path, destinations: list[DestinationConfig] | tuple[DestinationConfig, ...]) -> tuple[DestinationConfig, ...]:
    """Replace legacy/single destination config with the multi-destination array."""
    normalized = tuple(destinations)
    if not normalized:
        raise ValueError("至少需要配置一个转发机器人")
    seen: set[str] = set()
    for destination in normalized:
        if destination.bot_id in seen:
            raise ValueError(f"机器人 bot_id 重复：{destination.bot_id}")
        seen.add(destination.bot_id)

    path = path.resolve()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    starts = [
        index for index, line in enumerate(lines)
        if line.strip() in {"[destination]", "[[destinations]]"}
    ]
    if starts:
        start = starts[0]
        end = _table_end(lines, start)
        # Consume all adjacent destination array tables, not just the first one.
        while end < len(lines) and lines[end].strip() == "[[destinations]]":
            end = _table_end(lines, end)
        updated = lines[:start] + _serialize_destinations(normalized, newline) + lines[end:]
    else:
        insert_at = next(
            (index for index, line in enumerate(lines) if line.strip() == "[runtime]"),
            len(lines),
        )
        updated = lines[:insert_at] + _serialize_destinations(normalized, newline) + lines[insert_at:]
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(updated), encoding="utf-8")
    temp.replace(path)
    return normalized


def save_destination_group_openid(path: Path, bot_id: str, group_openid: str) -> None:
    """Update one bot target group while preserving all other bot settings."""
    if not bot_id.strip():
        raise ValueError("bot_id 不能为空")
    if not group_openid.strip():
        raise ValueError("group_openid 不能为空")
    config = load_config(path)
    updated = [
        DestinationConfig(
            app_id=item.app_id,
            client_secret_env=item.client_secret_env,
            group_openid=group_openid if item.bot_id == bot_id.strip() else item.group_openid,
            message_prefix=item.message_prefix,
            bot_id=item.bot_id,
        )
        for item in config.destinations
    ]
    if not any(item.bot_id == bot_id.strip() for item in config.destinations):
        raise ValueError(f"未找到机器人：{bot_id.strip()}")
    save_destinations(path, updated)


def save_source_backend(path: Path, backend: str) -> str:
    normalized = backend.strip().casefold()
    if normalized not in {"windows_notification", "napcat"}:
        raise ValueError("消息源必须是 windows_notification 或 napcat")
    _replace_table_option(path, "[source]", "backend", json.dumps(normalized))
    return normalized


def save_napcat_settings(
    path: Path,
    *,
    enabled: bool,
    ws_url: str,
    token_env: str,
) -> None:
    if not isinstance(enabled, bool):
        raise ValueError("napcat.enabled 必须是布尔值")
    if not ws_url.strip().startswith(("ws://", "wss://")):
        raise ValueError("NapCat 地址必须以 ws:// 或 wss:// 开头")
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_env.strip()):
        raise ValueError("NapCat Token 必须填写环境变量名")
    _replace_table_option(path, "[napcat]", "enabled", "true" if enabled else "false")
    _replace_table_option(path, "[napcat]", "ws_url", json.dumps(ws_url.strip()))
    _replace_table_option(path, "[napcat]", "token_env", json.dumps(token_env.strip()))


def save_listener_sessions(path: Path, sessions: list[ListenerSession] | tuple[ListenerSession, ...]) -> tuple[ListenerSession, ...]:
    normalized = tuple(sessions)
    seen: set[tuple[str, str]] = set()
    for session in normalized:
        identity = (session.type, session.id)
        if identity in seen:
            raise ValueError(f"监听会话重复：{session.type}/{session.id}")
        seen.add(identity)
    path = path.resolve()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    source_start = next((index for index, line in enumerate(lines) if line.strip() == "[source]"), None)
    if source_start is None:
        raise ValueError("config.toml 中未找到 [source] 配置段")
    source_end = next(
        (index for index in range(source_start + 1, len(lines))
         if lines[index].strip().startswith("[") and not lines[index].strip().startswith("[[") and lines[index].strip().endswith("]")),
        len(lines),
    )
    body: list[str] = []
    index = source_start + 1
    while index < source_end:
        if lines[index].strip() == "[[source.sessions]]":
            index += 1
            while index < source_end and not (lines[index].strip().startswith("[") and lines[index].strip().endswith("]")):
                index += 1
            continue
        body.append(lines[index])
        index += 1
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    for session in normalized:
        body.extend([
            f"[[source.sessions]]{newline}",
            f"type = {json.dumps(session.type)}{newline}",
            f"id = {json.dumps(session.id)}{newline}",
            f"name = {json.dumps(session.name, ensure_ascii=False)}{newline}",
            newline,
        ])
    updated = lines[:source_start + 1] + body + lines[source_end:]
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(updated), encoding="utf-8")
    temp.replace(path)
    return normalized
