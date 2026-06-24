from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database import (
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_READY_REVIEW,
    STATUS_WAITING_MANAGER,
    TaskDatabase,
)


def test_manager_to_assistant_lifecycle(tmp_path):
    db = TaskDatabase(tmp_path / "tasks.db")

    task = db.create_task(
        title="Подготовить договор",
        deadline="завтра 18:00",
        comment="проверить сумму",
        assistant_id=111,
        manager_id=222,
        created_by_id=222,
        created_by_role="manager",
    )

    assert task.status == STATUS_IN_PROGRESS
    assert task.manager_id == 222

    task = db.update_status(
        task.id,
        STATUS_IN_PROGRESS,
        assistant_comment="Приняла, нужен шаблон договора",
    )

    assert task.assistant_comment == "Приняла, нужен шаблон договора"

    task = db.submit_solution(
        task.id,
        manager_id=222,
        solution_text="Договор готов",
        solution_file_name="contract.docx",
        solution_file_type="local-file",
    )

    assert task.status == STATUS_WAITING_MANAGER
    assert task.solution_text == "Договор готов"

    task = db.update_status(task.id, STATUS_READY_REVIEW, manager_feedback="Поправить пункт 4")

    assert task.manager_feedback == "Поправить пункт 4"

    task = db.update_status(task.id, STATUS_DONE, manager_feedback="Одобрено")

    assert task.status == STATUS_DONE
