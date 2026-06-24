from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


load_dotenv()


def _read_ids(name: str) -> set[int]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return set()

    ids: set[int] = set()
    for item in raw_value.split(","):
        item = item.strip()
        if item:
            ids.add(int(item))
    return ids


@dataclass(frozen=True)
class Settings:
    bot_token: str
    assistant_ids: set[int]
    manager_ids: set[int]
    database_path: Path
    reminder_after_minutes: int
    digest_hour: int
    digest_minute: int


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Add BOT_TOKEN to your .env file.")

    return Settings(
        bot_token=token,
        assistant_ids=_read_ids("ASSISTANT_IDS"),
        manager_ids=_read_ids("MANAGER_IDS"),
        database_path=Path(os.getenv("DATABASE_PATH", "assistant_tasks.db")),
        reminder_after_minutes=int(os.getenv("REMINDER_AFTER_MINUTES", "60")),
        digest_hour=int(os.getenv("DIGEST_HOUR", "9")),
        digest_minute=int(os.getenv("DIGEST_MINUTE", "0")),
    )
