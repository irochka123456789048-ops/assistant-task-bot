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


def _read_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, "").strip().lower()
    if not raw_value:
        return default
    return raw_value in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    assistant_ids: set[int]
    manager_ids: set[int]
    group_chat_ids: set[int]
    database_path: Path
    reminder_after_minutes: int
    digest_hour: int
    digest_minute: int
    auto_create_tasks: bool
    require_assistant_confirmation: bool
    send_candidates_to_dm: bool
    silent_in_group: bool


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Add BOT_TOKEN to your .env file.")

    return Settings(
        bot_token=token,
        assistant_ids=_read_ids("ASSISTANT_IDS"),
        manager_ids=_read_ids("MANAGER_IDS"),
        group_chat_ids=_read_ids("GROUP_CHAT_IDS"),
        database_path=Path(os.getenv("DATABASE_PATH", "assistant_tasks.db")),
        reminder_after_minutes=int(os.getenv("REMINDER_AFTER_MINUTES", "60")),
        digest_hour=int(os.getenv("DIGEST_HOUR", "9")),
        digest_minute=int(os.getenv("DIGEST_MINUTE", "0")),
        auto_create_tasks=_read_bool("AUTO_CREATE_TASKS", False),
        require_assistant_confirmation=_read_bool("REQUIRE_ASSISTANT_CONFIRMATION", True),
        send_candidates_to_dm=_read_bool("SEND_CANDIDATES_TO_DM", True),
        silent_in_group=_read_bool("SILENT_IN_GROUP", True),
    )
