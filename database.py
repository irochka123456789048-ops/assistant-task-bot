from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


STATUS_IN_PROGRESS = "В работе"
STATUS_WAITING_MANAGER = "Жду решения"
STATUS_APPROVAL = "На согласовании"
STATUS_NEEDS_INPUT = "Жду комментарии"
STATUS_STUCK = "Зависло"
STATUS_POSTPONED = "Перенос"
STATUS_CANCELLED = "Отмена"
STATUS_DONE = "Выполнено"

ALL_STATUSES = (
    STATUS_IN_PROGRESS,
    STATUS_WAITING_MANAGER,
    STATUS_APPROVAL,
    STATUS_NEEDS_INPUT,
    STATUS_STUCK,
    STATUS_POSTPONED,
    STATUS_CANCELLED,
    STATUS_DONE,
)
MANAGER_PENDING_STATUSES = (
    STATUS_WAITING_MANAGER,
    STATUS_APPROVAL,
    STATUS_NEEDS_INPUT,
    STATUS_STUCK,
    STATUS_POSTPONED,
)


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    deadline: str
    comment: str
    status: str
    assistant_id: int
    manager_id: int | None
    created_by_id: int | None
    created_by_role: str
    created_at: str
    accepted_at: str | None
    submitted_at: str | None
    sent_at: str | None
    status_changed_at: str | None
    last_reminded_at: str | None
    assistant_comment: str
    manager_feedback: str
    solution_text: str
    solution_file_id: str
    solution_file_name: str
    solution_file_type: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    deadline TEXT NOT NULL DEFAULT '',
                    comment TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    assistant_id INTEGER NOT NULL,
                    manager_id INTEGER,
                    created_by_id INTEGER,
                    created_by_role TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    accepted_at TEXT,
                    submitted_at TEXT,
                    sent_at TEXT,
                    status_changed_at TEXT,
                    last_reminded_at TEXT,
                    assistant_comment TEXT NOT NULL DEFAULT '',
                    manager_feedback TEXT NOT NULL DEFAULT '',
                    solution_text TEXT NOT NULL DEFAULT '',
                    solution_file_id TEXT NOT NULL DEFAULT '',
                    solution_file_name TEXT NOT NULL DEFAULT '',
                    solution_file_type TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._add_column_if_missing(connection, "created_by_id", "INTEGER")
            self._add_column_if_missing(connection, "created_by_role", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing(connection, "accepted_at", "TEXT")
            self._add_column_if_missing(connection, "submitted_at", "TEXT")
            self._add_column_if_missing(connection, "status_changed_at", "TEXT")
            self._add_column_if_missing(connection, "assistant_comment", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing(connection, "manager_feedback", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing(connection, "solution_text", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing(connection, "solution_file_id", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing(connection, "solution_file_name", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_missing(connection, "solution_file_type", "TEXT NOT NULL DEFAULT ''")

    def _add_column_if_missing(self, connection: sqlite3.Connection, name: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if name not in columns:
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

    def create_task(
        self,
        title: str,
        deadline: str,
        comment: str,
        assistant_id: int,
        manager_id: int | None = None,
        created_by_id: int | None = None,
        created_by_role: str = "manager",
    ) -> Task:
        created_at = now_iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tasks (
                    title, deadline, comment, status, assistant_id, manager_id,
                    created_by_id, created_by_role, created_at, status_changed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    deadline,
                    comment,
                    STATUS_IN_PROGRESS,
                    assistant_id,
                    manager_id,
                    created_by_id,
                    created_by_role,
                    created_at,
                    created_at,
                ),
            )
            task_id = int(cursor.lastrowid)
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> Task:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task {task_id} not found")
        return self._task_from_row(row)

    def list_tasks(self, statuses: tuple[str, ...] | None = None) -> list[Task]:
        with self.connect() as connection:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id DESC",
                    statuses,
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tasks WHERE status NOT IN (?, ?) ORDER BY id DESC",
                    (STATUS_DONE, STATUS_CANCELLED),
                ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_all_tasks(self) -> list[Task]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
        return [self._task_from_row(row) for row in rows]

    def update_status(
        self,
        task_id: int,
        status: str,
        assistant_comment: str | None = None,
        manager_feedback: str | None = None,
    ) -> Task:
        timestamp = now_iso()
        fields = ["status = ?", "status_changed_at = ?"]
        values: list[object] = [status, timestamp]

        if assistant_comment is not None:
            fields.append("assistant_comment = ?")
            values.append(assistant_comment)
            if status == STATUS_IN_PROGRESS:
                fields.append("accepted_at = ?")
                values.append(timestamp)

        if manager_feedback is not None:
            fields.append("manager_feedback = ?")
            values.append(manager_feedback)

        if status in MANAGER_PENDING_STATUSES:
            fields.append("sent_at = ?")
            fields.append("last_reminded_at = NULL")
            values.append(timestamp)

        values.append(task_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
                values,
            )
        return self.get_task(task_id)

    def submit_solution(
        self,
        task_id: int,
        manager_id: int,
        solution_text: str = "",
        solution_file_id: str = "",
        solution_file_name: str = "",
        solution_file_type: str = "",
    ) -> Task:
        timestamp = now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?,
                    manager_id = ?,
                    submitted_at = ?,
                    sent_at = ?,
                    status_changed_at = ?,
                    last_reminded_at = NULL,
                    solution_text = ?,
                    solution_file_id = ?,
                    solution_file_name = ?,
                    solution_file_type = ?
                WHERE id = ?
                """,
                (
                    STATUS_APPROVAL,
                    manager_id,
                    timestamp,
                    timestamp,
                    timestamp,
                    solution_text,
                    solution_file_id,
                    solution_file_name,
                    solution_file_type,
                    task_id,
                ),
            )
        return self.get_task(task_id)

    def mark_reminded(self, task_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE tasks SET last_reminded_at = ? WHERE id = ?",
                (now_iso(), task_id),
            )

    def count_by_status(self) -> list[tuple[str, int]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status ORDER BY status"
            ).fetchall()
        return [(str(row["status"]), int(row["count"])) for row in rows]

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        return Task(
            id=int(row["id"]),
            title=str(row["title"]),
            deadline=str(row["deadline"]),
            comment=str(row["comment"]),
            status=str(row["status"]),
            assistant_id=int(row["assistant_id"]),
            manager_id=int(row["manager_id"]) if row["manager_id"] is not None else None,
            created_by_id=int(row["created_by_id"]) if row["created_by_id"] is not None else None,
            created_by_role=str(row["created_by_role"] or ""),
            created_at=str(row["created_at"]),
            accepted_at=str(row["accepted_at"]) if row["accepted_at"] is not None else None,
            submitted_at=str(row["submitted_at"]) if row["submitted_at"] is not None else None,
            sent_at=str(row["sent_at"]) if row["sent_at"] is not None else None,
            status_changed_at=str(row["status_changed_at"]) if "status_changed_at" in row.keys() and row["status_changed_at"] is not None else None,
            last_reminded_at=str(row["last_reminded_at"]) if row["last_reminded_at"] is not None else None,
            assistant_comment=str(row["assistant_comment"] or ""),
            manager_feedback=str(row["manager_feedback"] or ""),
            solution_text=str(row["solution_text"] or ""),
            solution_file_id=str(row["solution_file_id"] or ""),
            solution_file_name=str(row["solution_file_name"] or ""),
            solution_file_type=str(row["solution_file_type"] or ""),
        )
