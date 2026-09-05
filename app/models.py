from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class IncomingMessage:
    message_key: str
    source_group: str
    content: str
    sender: str | None = None
    kind: str = "text"
    observed_at: str = ""
    media_path: str | None = None

    @classmethod
    def create(
        cls,
        message_key: str,
        source_group: str,
        content: str,
        sender: str | None = None,
        kind: str = "text",
        media_path: str | None = None,
    ) -> "IncomingMessage":
        return cls(
            message_key=message_key,
            source_group=source_group,
            content=content,
            sender=sender,
            kind=kind,
            observed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            media_path=media_path,
        )
