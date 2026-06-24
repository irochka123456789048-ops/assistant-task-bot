from __future__ import annotations

from pathlib import Path

from database import (
    ALL_STATUSES,
    STATUS_DONE,
    STATUS_NEEDS_INPUT,
    STATUS_READY_REVIEW,
    STATUS_STUCK,
    Task,
    TaskDatabase,
)


DEFAULT_ASSISTANT_ID = 1
DEFAULT_MANAGER_ID = 2


def read_database_path() -> Path:
    env_path = Path(".env")
    if not env_path.exists():
        return Path("assistant_tasks.db")

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "DATABASE_PATH" and value.strip():
            return Path(value.strip())

    return Path("assistant_tasks.db")


def print_task(task: Task) -> None:
    lines = [
        f"#{task.id} {task.title}",
        f"   Дедлайн: {task.deadline}",
        f"   Статус: {task.status}",
    ]
    if task.comment:
        lines.append(f"   Задача от руководителя: {task.comment}")
    if task.assistant_comment:
        lines.append(f"   Комментарий ассистента: {task.assistant_comment}")
    if task.manager_feedback:
        lines.append(f"   Комментарий руководителя: {task.manager_feedback}")
    if task.solution_text:
        lines.append(f"   Результат: {task.solution_text}")
    if task.solution_file_name:
        lines.append(f"   Файл результата: {task.solution_file_name}")
    print("\n".join(lines))


def ask_task_id() -> int | None:
    raw_value = input("Введите номер задачи: ").strip()
    if not raw_value.isdigit():
        print("Нужен номер задачи, например: 1")
        return None
    return int(raw_value)


def manager_create_task(db: TaskDatabase) -> None:
    title = input("Название задачи: ").strip()
    deadline = input("Дедлайн: ").strip()
    comment = input("Комментарий руководителя: ").strip()

    if not title or not deadline:
        print("Название и дедлайн обязательны.")
        return

    task = db.create_task(
        title=title,
        deadline=deadline,
        comment=comment,
        assistant_id=DEFAULT_ASSISTANT_ID,
        manager_id=DEFAULT_MANAGER_ID,
        created_by_id=DEFAULT_MANAGER_ID,
        created_by_role="manager",
    )
    print("\nРуководитель создал задачу. В Telegram она прилетела бы ассистенту:")
    print_task(task)


def assistant_choose_status(db: TaskDatabase) -> None:
    task_id = ask_task_id()
    if task_id is None:
        return

    print("\nВыберите статус ассистента:")
    for index, status in enumerate(ALL_STATUSES, start=1):
        print(f"{index}. {status}")

    raw_choice = input("Номер статуса: ").strip()
    if not raw_choice.isdigit():
        print("Нужен номер статуса.")
        return

    choice = int(raw_choice)
    if choice < 1 or choice > len(ALL_STATUSES):
        print("Такого статуса нет.")
        return

    comment = input("Комментарий ассистента руководителю: ").strip()

    try:
        task = db.update_status(task_id, ALL_STATUSES[choice - 1], assistant_comment=comment)
    except KeyError:
        print("Такой задачи нет.")
        return

    print("\nСтатус ассистента сохранён. Руководитель получил бы уведомление:")
    print_task(task)


def assistant_submit_solution(db: TaskDatabase) -> None:
    task_id = ask_task_id()
    if task_id is None:
        return

    solution_text = input("Что сделано / результат текстом: ").strip()
    file_name = input("Название файла или фото, если есть: ").strip()

    if not solution_text and not file_name:
        print("Нужно добавить текст результата или файл.")
        return

    try:
        task = db.submit_solution(
            task_id=task_id,
            manager_id=DEFAULT_MANAGER_ID,
            solution_text=solution_text,
            solution_file_name=file_name,
            solution_file_type="local-file" if file_name else "",
        )
    except KeyError:
        print("Такой задачи нет.")
        return

    print("\nАссистент сдал результат. Руководитель увидел бы кнопки решения:")
    print("Одобрить / Нужны правки / Задать вопрос / Позже")
    print_task(task)


def manager_feedback(db: TaskDatabase) -> None:
    task_id = ask_task_id()
    if task_id is None:
        return

    decisions = [
        ("Одобрить", STATUS_DONE),
        ("Нужны правки", STATUS_READY_REVIEW),
        ("Задать вопрос", STATUS_NEEDS_INPUT),
        ("Позже", STATUS_STUCK),
    ]

    print("\nРешение руководителя:")
    for index, (title, status) in enumerate(decisions, start=1):
        print(f"{index}. {title} -> {status}")

    raw_choice = input("Номер решения: ").strip()
    if not raw_choice.isdigit():
        print("Нужен номер решения.")
        return

    choice = int(raw_choice)
    if choice < 1 or choice > len(decisions):
        print("Такого решения нет.")
        return

    _, status = decisions[choice - 1]
    feedback = "Одобрено" if status == STATUS_DONE else input("Комментарий руководителя: ").strip()

    try:
        task = db.update_status(task_id, status, manager_feedback=feedback)
    except KeyError:
        print("Такой задачи нет.")
        return

    print("\nОбратная связь руководителя отправлена ассистенту:")
    print_task(task)


def list_tasks(db: TaskDatabase) -> None:
    tasks = db.list_tasks()
    if not tasks:
        print("Активных задач пока нет.")
        return

    print("\nАктивные задачи:")
    for task in tasks:
        print()
        print_task(task)


def show_summary(db: TaskDatabase) -> None:
    rows = db.count_by_status()
    if not rows:
        print("Пока задач нет.")
        return

    print("\nДневной summary:")
    for status, count in rows:
        print(f"{status}: {count}")


def print_menu(database_path: Path) -> None:
    print("\nЛокальная проверка MVP без Telegram")
    print(f"База данных: {database_path}")
    print("1. Руководитель создаёт задачу")
    print("2. Ассистент выбирает статус и пишет комментарий")
    print("3. Ассистент сдаёт результат текстом или файлом")
    print("4. Руководитель выбирает решение и пишет комментарий")
    print("5. Показать список задач")
    print("6. Показать дневной summary")
    print("0. Выйти")


def main() -> None:
    db = TaskDatabase(read_database_path())

    while True:
        print_menu(db.path)
        choice = input("Выберите пункт: ").strip()

        if choice == "1":
            manager_create_task(db)
        elif choice == "2":
            assistant_choose_status(db)
        elif choice == "3":
            assistant_submit_solution(db)
        elif choice == "4":
            manager_feedback(db)
        elif choice == "5":
            list_tasks(db)
        elif choice == "6":
            show_summary(db)
        elif choice == "0":
            print("Готово. Можно закрывать окно.")
            break
        else:
            print("Выберите цифру из меню.")


if __name__ == "__main__":
    main()
