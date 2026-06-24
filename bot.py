from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, time, timezone
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings, load_settings
from database import (
    MANAGER_PENDING_STATUSES,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NEEDS_INPUT,
    STATUS_READY_REVIEW,
    STATUS_STUCK,
    STATUS_WAITING_MANAGER,
    Task,
    TaskDatabase,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

STATUS_BY_KEY = {
    "work": STATUS_IN_PROGRESS,
    "wait": STATUS_WAITING_MANAGER,
    "review": STATUS_READY_REVIEW,
    "input": STATUS_NEEDS_INPUT,
    "stuck": STATUS_STUCK,
    "done": STATUS_DONE,
}


def is_assistant(user_id: int, settings: Settings) -> bool:
    return user_id in settings.assistant_ids


def is_manager(user_id: int, settings: Settings) -> bool:
    return user_id in settings.manager_ids


def pick_assistant_id(settings: Settings) -> int | None:
    if not settings.assistant_ids:
        return None
    return next(iter(settings.assistant_ids))


def task_text(task: Task, include_solution: bool = True) -> str:
    lines = [
        f"#{task.id} {task.title}",
        f"Дедлайн: {task.deadline}",
        f"Статус: {task.status}",
    ]

    if task.comment:
        lines.append(f"Задача от руководителя: {task.comment}")
    if task.assistant_comment:
        lines.append(f"Комментарий ассистента: {task.assistant_comment}")
    if task.manager_feedback:
        lines.append(f"Комментарий руководителя: {task.manager_feedback}")
    if include_solution and task.solution_text:
        lines.append(f"Результат: {task.solution_text}")
    if include_solution and task.solution_file_name:
        lines.append(f"Файл результата: {task.solution_file_name}")

    return "\n".join(lines)


def assistant_status_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("В работе", callback_data=f"assistant_status:work:{task_id}"),
                InlineKeyboardButton("Ждёт руководителя", callback_data=f"assistant_status:wait:{task_id}"),
            ],
            [
                InlineKeyboardButton("Готово на проверку", callback_data=f"assistant_status:review:{task_id}"),
                InlineKeyboardButton("Нужны вводные", callback_data=f"assistant_status:input:{task_id}"),
            ],
            [
                InlineKeyboardButton("Зависло", callback_data=f"assistant_status:stuck:{task_id}"),
                InlineKeyboardButton("Выполнено", callback_data=f"assistant_status:done:{task_id}"),
            ],
        ]
    )


def manager_decision_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Одобрить", callback_data=f"manager_decision:approve:{task_id}"),
                InlineKeyboardButton("Нужны правки", callback_data=f"manager_decision:changes:{task_id}"),
            ],
            [
                InlineKeyboardButton("Задать вопрос", callback_data=f"manager_decision:question:{task_id}"),
                InlineKeyboardButton("Позже", callback_data=f"manager_decision:later:{task_id}"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Здравствуйте! Я бот для задач руководителя и ассистента.\n\n"
        "Руководитель ставит задачу:\n"
        "/new Название | дедлайн | комментарий\n\n"
        "Ассистент получает задачу, выбирает статус кнопкой и пишет комментарий.\n"
        "Когда задача готова, ассистент сдаёт результат:\n"
        "/submit ID | результат\n\n"
        "Полезные команды:\n"
        "/whoami - показать ваш Telegram ID\n"
        "/list - активные задачи\n"
        "/waiting - задачи, которые ждут руководителя\n"
        "/summary - сводка"
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def new_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    user_id = update.effective_user.id

    if not is_manager(user_id, settings):
        await update.message.reply_text("Создавать задачи может только руководитель.")
        return

    assistant_id = pick_assistant_id(settings)
    if assistant_id is None:
        await update.message.reply_text("В .env не указан ASSISTANT_IDS.")
        return

    text = update.message.text.removeprefix("/new").strip()
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await update.message.reply_text(
            "Напишите так:\n/new Название задачи | дедлайн | комментарий\n\n"
            "Пример:\n/new Подготовить договор | завтра 18:00 | проверить сумму"
        )
        return

    task = db.create_task(
        title=parts[0],
        deadline=parts[1],
        comment=parts[2] if len(parts) >= 3 else "",
        assistant_id=assistant_id,
        manager_id=user_id,
        created_by_id=user_id,
        created_by_role="manager",
    )

    await update.message.reply_text(f"Задача создана и отправлена ассистенту:\n\n{task_text(task)}")
    await context.bot.send_message(
        chat_id=assistant_id,
        text="Новая задача от руководителя. Выберите статус и затем напишите комментарий:\n\n"
        f"{task_text(task)}",
        reply_markup=assistant_status_keyboard(task.id),
    )


async def submit_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    user_id = update.effective_user.id

    if not is_assistant(user_id, settings):
        await update.message.reply_text("Сдавать результат может только ассистент.")
        return

    parsed = parse_submit_message(update.message)
    if parsed is None:
        await update.message.reply_text(
            "Напишите так:\n/submit 1 | что сделано\n\n"
            "Файл тоже можно отправить: прикрепите документ или фото и добавьте подпись\n"
            "/submit 1 | комментарий к результату"
        )
        return

    task_id, solution_text, file_id, file_name, file_type = parsed

    try:
        old_task = db.get_task(task_id)
        manager_id = old_task.manager_id or old_task.created_by_id
        if manager_id is None:
            raise KeyError
        task = db.submit_solution(
            task_id=task_id,
            manager_id=manager_id,
            solution_text=solution_text,
            solution_file_id=file_id,
            solution_file_name=file_name,
            solution_file_type=file_type,
        )
    except KeyError:
        await update.message.reply_text("Такой задачи нет или у неё не указан руководитель.")
        return

    await send_result_to_manager(context, manager_id, task)
    await update.message.reply_text(f"Результат отправлен руководителю:\n\n{task_text(task)}")


def parse_submit_message(message: Message) -> tuple[int, str, str, str, str] | None:
    text = message.caption or message.text or ""
    text = text.removeprefix("/submit").strip()
    parts = [part.strip() for part in text.split("|", 1)]

    if not parts or not parts[0].isdigit():
        return None

    task_id = int(parts[0])
    solution_text = parts[1] if len(parts) > 1 else ""
    file_id = ""
    file_name = ""
    file_type = ""

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "document"
        file_type = "document"
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_name = "photo"
        file_type = "photo"

    if not solution_text and not file_id:
        return None

    return task_id, solution_text, file_id, file_name, file_type


async def send_result_to_manager(context: ContextTypes.DEFAULT_TYPE, manager_id: int, task: Task) -> None:
    text = "Ассистент сдал результат. Выберите решение:\n\n" f"{task_text(task)}"

    if task.solution_file_id and task.solution_file_type == "document":
        await context.bot.send_document(
            chat_id=manager_id,
            document=task.solution_file_id,
            caption=text,
            reply_markup=manager_decision_keyboard(task.id),
        )
        return

    if task.solution_file_id and task.solution_file_type == "photo":
        await context.bot.send_photo(
            chat_id=manager_id,
            photo=task.solution_file_id,
            caption=text,
            reply_markup=manager_decision_keyboard(task.id),
        )
        return

    await context.bot.send_message(
        chat_id=manager_id,
        text=text,
        reply_markup=manager_decision_keyboard(task.id),
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    action_group = parts[0]

    if action_group == "assistant_status":
        await handle_assistant_status_button(update, context, settings, parts)
        return

    if action_group == "manager_decision":
        await handle_manager_decision_button(update, context, settings, parts)
        return


async def handle_assistant_status_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    parts: list[str],
) -> None:
    if not is_assistant(update.callback_query.from_user.id, settings):
        await update.callback_query.edit_message_text("Эти кнопки доступны только ассистенту.")
        return

    _, status_key, task_id_text = parts
    status = STATUS_BY_KEY[status_key]
    task_id = int(task_id_text)
    pending = context.application.bot_data.setdefault("pending_assistant_comments", {})
    pending[update.callback_query.from_user.id] = (task_id, status)

    await update.callback_query.edit_message_text(
        f"Вы выбрали статус: {status}\n\n"
        "Теперь напишите комментарий к руководителю одним сообщением.\n"
        "Если комментарий не нужен, напишите: -"
    )


async def handle_manager_decision_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    parts: list[str],
) -> None:
    db: TaskDatabase = context.application.bot_data["db"]
    query = update.callback_query

    if not is_manager(query.from_user.id, settings):
        await query.edit_message_text("Эти кнопки доступны только руководителю.")
        return

    _, decision, task_id_text = parts
    task_id = int(task_id_text)

    if decision == "approve":
        task = db.update_status(task_id, STATUS_DONE, manager_feedback="Одобрено")
        await query.edit_message_text(f"Задача одобрена:\n\n{task_text(task)}")
        await context.bot.send_message(
            chat_id=task.assistant_id,
            text=f"Руководитель одобрил задачу #{task.id}.\n\n{task_text(task)}",
        )
        return

    status_by_decision = {
        "changes": STATUS_READY_REVIEW,
        "question": STATUS_NEEDS_INPUT,
        "later": STATUS_STUCK,
    }
    status = status_by_decision[decision]
    pending = context.application.bot_data.setdefault("pending_manager_feedback", {})
    pending[query.from_user.id] = (task_id, status)

    await query.edit_message_text(
        f"Вы выбрали статус: {status}\n\n"
        "Теперь напишите комментарий для ассистента: правки, вопрос или причину, почему позже."
    )


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    db: TaskDatabase = context.application.bot_data["db"]

    pending_assistant = context.application.bot_data.setdefault("pending_assistant_comments", {})
    if user_id in pending_assistant:
        task_id, status = pending_assistant.pop(user_id)
        comment = "" if text == "-" else text
        try:
            task = db.update_status(task_id, status, assistant_comment=comment)
        except KeyError:
            await update.message.reply_text("Такой задачи нет.")
            return

        await update.message.reply_text(f"Статус сохранён:\n\n{task_text(task)}")
        if task.manager_id:
            await context.bot.send_message(
                chat_id=task.manager_id,
                text=f"Ассистент обновил статус задачи #{task.id}:\n\n{task_text(task)}",
            )
        return

    pending_manager = context.application.bot_data.setdefault("pending_manager_feedback", {})
    if user_id in pending_manager:
        task_id, status = pending_manager.pop(user_id)
        try:
            task = db.update_status(task_id, status, manager_feedback=text)
        except KeyError:
            await update.message.reply_text("Такой задачи нет.")
            return

        await update.message.reply_text(f"Комментарий отправлен ассистенту:\n\n{task_text(task)}")
        await context.bot.send_message(
            chat_id=task.assistant_id,
            text=f"Руководитель дал обратную связь по задаче #{task.id}:\n\n{task_text(task)}",
        )


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: TaskDatabase = context.application.bot_data["db"]
    await update.message.reply_text(format_task_list(db.list_tasks(), "Активные задачи"))


async def waiting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: TaskDatabase = context.application.bot_data["db"]
    await update.message.reply_text(format_task_list(db.list_tasks(MANAGER_PENDING_STATUSES), "Ждут руководителя"))


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: TaskDatabase = context.application.bot_data["db"]
    rows = db.count_by_status()
    if not rows:
        await update.message.reply_text("Пока задач нет.")
        return
    await update.message.reply_text("Сводка:\n" + "\n".join(f"{status}: {count}" for status, count in rows))


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: TaskDatabase = context.application.bot_data["db"]
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Напишите номер задачи: /done 1")
        return

    try:
        task = db.update_status(int(context.args[0]), STATUS_DONE)
    except KeyError:
        await update.message.reply_text("Такой задачи нет.")
        return
    await update.message.reply_text(f"Готово:\n\n{task_text(task)}")


async def remind_managers(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    now = datetime.now(timezone.utc)
    remind_after = timedelta(minutes=settings.reminder_after_minutes)

    for task in db.list_tasks(MANAGER_PENDING_STATUSES):
        if not task.manager_id or not task.sent_at:
            continue

        last_touch = task.last_reminded_at or task.sent_at
        last_touch_dt = datetime.fromisoformat(last_touch)
        if now - last_touch_dt < remind_after:
            continue

        await context.bot.send_message(
            chat_id=task.manager_id,
            text=f"Напоминание: задача ждёт вашего решения.\n\n{task_text(task)}",
            reply_markup=manager_decision_keyboard(task.id),
        )
        db.mark_reminded(task.id)


async def morning_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    text = format_task_list(db.list_tasks()[:10], "Утренний дайджест")
    for manager_id in settings.manager_ids:
        await context.bot.send_message(chat_id=manager_id, text=text)


def format_task_list(tasks: list[Task], title: str) -> str:
    if not tasks:
        return f"{title}: пусто."
    return f"{title}:\n\n" + "\n\n".join(task_text(task) for task in tasks)


def build_application(settings: Settings | None = None, db: TaskDatabase | None = None) -> Application:
    settings = settings or load_settings()
    db = db or TaskDatabase(settings.database_path)

    application = Application.builder().token(settings.bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("new", new_task))
    application.add_handler(CommandHandler("submit", submit_task))
    application.add_handler(CommandHandler("list", list_tasks))
    application.add_handler(CommandHandler("waiting", waiting))
    application.add_handler(CommandHandler("summary", summary))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(MessageHandler(filters.CaptionRegex(r"^/submit"), submit_task))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))

    if application.job_queue:
        application.job_queue.run_repeating(remind_managers, interval=60, first=60)
        application.job_queue.run_daily(
            morning_digest,
            time=time(hour=settings.digest_hour, minute=settings.digest_minute),
        )

    return application


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
