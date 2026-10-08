from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path

TARGET_QUEUE_SIZE = 10
POSTS_PER_DAY = 5  # Buffer owns the actual posting times.


@dataclass(frozen=True)
class Config:
    dry_run: bool
    buffer_write_enabled: bool
    database: Path
    buffer_api_key: str | None
    buffer_org_id: str | None
    buffer_channel_id: str | None

    @classmethod
    def from_env(cls, root: Path) -> "Config":
        def boolean(name: str, default: str) -> bool:
            raw = os.getenv(name, default).strip().lower()
            if raw not in {"true", "1", "false", "0"}:
                raise ValueError(f"{name} must be true or false")
            return raw in {"true", "1"}

        db = Path(os.getenv("FIGURESRADAR_DB", "data/figuresradar.sqlite3"))
        target = json.loads((root / "config" / "buffer-target.json").read_text(encoding="utf-8"))
        return cls(
            dry_run=boolean("DRY_RUN", "true"),
            buffer_write_enabled=boolean("BUFFER_WRITE_ENABLED", "false"),
            database=db if db.is_absolute() else root / db,
            buffer_api_key=os.getenv("BUFFER_API_KEY") or None,
            buffer_org_id=str(target["organization_id"]),
            buffer_channel_id=str(target["channel_id"]),
        )

    def assert_writes_allowed(self) -> None:
        if self.dry_run or not self.buffer_write_enabled:
            raise RuntimeError("Buffer writes require DRY_RUN=false and BUFFER_WRITE_ENABLED=true")


def needed_posts(pending_buffer_posts: int) -> int:
    if pending_buffer_posts < 0:
        raise ValueError("Pending count cannot be negative")
    return max(0, TARGET_QUEUE_SIZE - pending_buffer_posts)
