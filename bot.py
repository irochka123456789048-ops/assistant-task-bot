from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, time, timezone
import logging
import os
from pathlib import Path
import tempfile
import time as time_module

from openai import OpenAI
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
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
    STATUS_CANCELLED,
    STATUS_APPROVAL,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NEEDS_INPUT,
    STATUS_POSTPONED,
    STATUS_STUCK,
    STATUS_WAITING_MANAGER,
    Task,
    TaskDatabase,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

MSK = timezone(timedelta(hours=3))

STATUS_BY_KEY = {
    "work": STATUS_IN_PROGRESS,
    "wait": STATUS_WAITING_MANAGER,
    "approval": STATUS_APPROVAL,
    "input": STATUS_NEEDS_INPUT,
    "stuck": STATUS_STUCK,
    "postponed": STATUS_POSTPONED,
    "cancelled": STATUS_CANCELLED,
    "done": STATUS_DONE,
}

MENU_NEW_TASK = "РЎРѕР·РґР°С‚СЊ Р·Р°РґР°С‡Сѓ"
MENU_LIST = "РЎРїРёСЃРѕРє Р·Р°РґР°С‡"
MENU_WAITING = "Р–РґСѓС‚ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ"
MENU_MANAGER_WAITING = "Р–РґСѓС‚ СЂРµС€РµРЅРёСЏ"
MENU_SUMMARY = "РЎРІРѕРґРєР°"
MENU_MANAGER_SUMMARY = "РЎРІРѕРґРєР° РїРѕ Р·Р°РґР°С‡Р°Рј"
MENU_SUBMIT = "РЎРґР°С‚СЊ СЂРµР·СѓР»СЊС‚Р°С‚"
MENU_DONE_TASKS = "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°С‡Рё"
MENU_WHOAMI = "РњРѕР№ Telegram ID"
MENU_CANCEL = "РћС‚РјРµРЅР°"
BACK_BUTTON = "в¬…пёЏ РќР°Р·Р°Рґ"

STATUS_LABELS = {
    STATUS_DONE: "🟢 Выполнено",
    STATUS_STUCK: "🔴 Зависло",
    STATUS_IN_PROGRESS: "🟡 В работе",
    STATUS_WAITING_MANAGER: "🔵 Жду решения",
    STATUS_APPROVAL: "🟣 На согласовании",
    STATUS_NEEDS_INPUT: "❔ Жду комментарии",
    STATUS_POSTPONED: "🟠 Перенос",
    STATUS_CANCELLED: "⚫ Отмена",
}

MANAGER_DECISION_LABELS = {
    "comment": "💬 Дать комментарий",
    "done": "🟢 Выполнено",
    "revision": "🟡 На доработку",
    "cancelled": "⚫ Отмена",
    "postponed": "🟠 Перенос",
}


def is_assistant(user_id: int, settings: Settings) -> bool:
    return user_id in settings.assistant_ids


def is_manager(user_id: int, settings: Settings) -> bool:
    return user_id in settings.manager_ids


def pick_assistant_id(settings: Settings) -> int | None:
    if not settings.assistant_ids:
        return None
    return next(iter(settings.assistant_ids))


def pick_manager_id(settings: Settings) -> int | None:
    if not settings.manager_ids:
        return None
    return next(iter(settings.manager_ids))


def main_menu(user_id: int, settings: Settings) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton | str]] = []
    if is_manager(user_id, settings):
        rows.append([MENU_NEW_TASK])
        rows.append([MENU_MANAGER_WAITING])
        rows.append([MENU_MANAGER_SUMMARY])
        rows.append([MENU_DONE_TASKS])
    if is_assistant(user_id, settings):
        if not is_manager(user_id, settings):
            rows.append([MENU_NEW_TASK])
        rows.append([MENU_SUBMIT])
        rows.append([MENU_WAITING])
        rows.append([MENU_SUMMARY])
    if not rows:
        rows.append([MENU_LIST])
        rows.append([MENU_SUMMARY])
    rows.append([MENU_WHOAMI])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[MENU_CANCEL]], resize_keyboard=True)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def format_datetime(value: str | None) -> str:
    if not value:
        return "РЅРµ СѓРєР°Р·Р°РЅРѕ"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def short_task_button(task: Task) -> str:
    task_time = task.submitted_at or task.status_changed_at or task.created_at
    return f"#{task.id} {task.title[:32]} | {status_label(task.status)} | {format_datetime(task_time)}"


def tasks_for_status(db: TaskDatabase, status: str, include_closed: bool = False) -> list[Task]:
    if include_closed:
        return [task for task in db.list_all_tasks() if task.status == status]
    return db.list_tasks((status,))


def task_text(task: Task, include_solution: bool = True) -> str:
    lines = [
        f"#{task.id} {task.title}",
        f"РЎС‚Р°С‚СѓСЃ: {status_label(task.status)}",
        f"РџРѕСЃС‚СѓРїРёР»Р°: {format_datetime(task.created_at)}",
        f"РЎС‚Р°С‚СѓСЃ РёР·РјРµРЅС‘РЅ: {format_datetime(task.status_changed_at or task.created_at)}",
    ]

    if task.deadline:
        lines.insert(1, f"Р”РµРґР»Р°Р№РЅ: {task.deadline}")

    if task.comment:
        lines.append(f"Р—Р°РґР°С‡Р° РѕС‚ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ: {task.comment}")
    if task.assistant_comment:
        lines.append(f"РљРѕРјРјРµРЅС‚Р°СЂРёР№ Р°СЃСЃРёСЃС‚РµРЅС‚Р°: {task.assistant_comment}")
    if task.manager_feedback:
        lines.append(f"РљРѕРјРјРµРЅС‚Р°СЂРёР№ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ: {task.manager_feedback}")
    if include_solution and task.solution_text:
        lines.append(f"Р РµР·СѓР»СЊС‚Р°С‚: {task.solution_text}")
    if include_solution and task.solution_file_name:
        lines.append(f"Р¤Р°Р№Р» СЂРµР·СѓР»СЊС‚Р°С‚Р°: {task.solution_file_name}")
    if task.submitted_at:
        lines.append(f"РЎРґР°РЅР° Р°СЃСЃРёСЃС‚РµРЅС‚РѕРј: {format_datetime(task.submitted_at)}")
    if task.status == STATUS_DONE:
        lines.append(f"РџРµСЂРµС€Р»Р° РІ РІС‹РїРѕР»РЅРµРЅРЅРѕРµ: {format_datetime(task.status_changed_at or task.submitted_at)}")

    return "\n".join(lines)


def assistant_status_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(status_label(STATUS_IN_PROGRESS), callback_data=f"assistant_status:work:{task_id}"),
                InlineKeyboardButton(status_label(STATUS_WAITING_MANAGER), callback_data=f"assistant_status:wait:{task_id}"),
            ],
            [
                InlineKeyboardButton(status_label(STATUS_APPROVAL), callback_data=f"assistant_status:approval:{task_id}"),
                InlineKeyboardButton(status_label(STATUS_NEEDS_INPUT), callback_data=f"assistant_status:input:{task_id}"),
            ],
            [
                InlineKeyboardButton(status_label(STATUS_STUCK), callback_data=f"assistant_status:stuck:{task_id}"),
                InlineKeyboardButton(status_label(STATUS_POSTPONED), callback_data=f"assistant_status:postponed:{task_id}"),
            ],
            [
                InlineKeyboardButton(status_label(STATUS_CANCELLED), callback_data=f"assistant_status:cancelled:{task_id}"),
                InlineKeyboardButton(status_label(STATUS_DONE), callback_data=f"assistant_status:done:{task_id}"),
            ],
        ]
    )


def manager_decision_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(MANAGER_DECISION_LABELS["comment"], callback_data=f"manager_decision:comment:{task_id}"),
            ],
            [
                InlineKeyboardButton(MANAGER_DECISION_LABELS["done"], callback_data=f"manager_decision:done:{task_id}"),
                InlineKeyboardButton(MANAGER_DECISION_LABELS["revision"], callback_data=f"manager_decision:revision:{task_id}"),
            ],
            [
                InlineKeyboardButton(MANAGER_DECISION_LABELS["cancelled"], callback_data=f"manager_decision:cancelled:{task_id}"),
                InlineKeyboardButton(MANAGER_DECISION_LABELS["postponed"], callback_data=f"manager_decision:postponed:{task_id}"),
            ],
        ]
    )


def manager_feedback_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Р”Р°С‚СЊ РєРѕРјРјРµРЅС‚Р°СЂРёР№", callback_data=f"manager_comment:{task_id}")]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user_id = update.effective_user.id
    await update.message.reply_text(
        "Р—РґСЂР°РІСЃС‚РІСѓР№С‚Рµ! РЇ Р±РѕС‚ РґР»СЏ Р·Р°РґР°С‡ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ Рё Р°СЃСЃРёСЃС‚РµРЅС‚Р°.\n\n"
        "РўРµРїРµСЂСЊ РјРѕР¶РЅРѕ РїРѕР»СЊР·РѕРІР°С‚СЊСЃСЏ РєРЅРѕРїРєР°РјРё РІРЅРёР·Сѓ СЌРєСЂР°РЅР°.\n"
        "Р СѓРєРѕРІРѕРґРёС‚РµР»СЊ СЃРѕР·РґР°С‘С‚ Р·Р°РґР°С‡Сѓ РєРЅРѕРїРєРѕР№, Р°СЃСЃРёСЃС‚РµРЅС‚ РїРѕР»СѓС‡Р°РµС‚ РµС‘ Рё РІС‹Р±РёСЂР°РµС‚ СЃС‚Р°С‚СѓСЃ.",
        reply_markup=main_menu(user_id, settings),
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    await update.message.reply_text(
        f"Р’Р°С€ Telegram ID: {update.effective_user.id}",
        reply_markup=main_menu(update.effective_user.id, settings),
    )


async def check_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api_key = get_yandex_api_key()
    folder_id = os.getenv("YANDEX_FOLDER_ID", "").strip()
    if not api_key:
        await update.message.reply_text("YANDEX_API_KEY РЅРµ РЅР°Р№РґРµРЅ РІ РїРµСЂРµРјРµРЅРЅС‹С… РѕРєСЂСѓР¶РµРЅРёСЏ Bothost.")
        return

    masked_key = f"{api_key[:7]}...{api_key[-4:]}" if len(api_key) > 12 else "РєР»СЋС‡ СЃР»РёС€РєРѕРј РєРѕСЂРѕС‚РєРёР№"
    folder_text = folder_id or "РЅРµ СѓРєР°Р·Р°РЅ, Р±РѕС‚ РІРѕР·СЊРјРµС‚ РєР°С‚Р°Р»РѕРі РёР· API-РєР»СЋС‡Р°"
    await update.message.reply_text(
        "Yandex SpeechKit РЅР°СЃС‚СЂРѕРµРЅ.\n"
        f"РњР°СЃРєР° РєР»СЋС‡Р°: {masked_key}\n\n"
        f"YANDEX_FOLDER_ID: {folder_text}\n\n"
        "РўРµРїРµСЂСЊ РѕС‚РїСЂР°РІСЊС‚Рµ РіРѕР»РѕСЃРѕРІРѕРµ РІ РіСЂСѓРїРїСѓ. Р•СЃР»Рё Р±СѓРґРµС‚ РѕС€РёР±РєР°, Р±РѕС‚ РїРѕРєР°Р¶РµС‚ РєРѕСЂРѕС‚РєСѓСЋ РїСЂРёС‡РёРЅСѓ."
    )


async def new_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.removeprefix("/new").strip()
    await create_task_from_text(update, context, text)


async def group_task_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.message
    if message is None or message.text is None:
        return

    if message.from_user is None or not is_manager(message.from_user.id, settings):
        return

    text = message.text.strip()
    if not text:
        return

    await create_task_from_text(update, context, text, source_chat_id=message.chat_id)


async def group_voice_task_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.message
    if message is None or message.voice is None:
        return

    if message.from_user is None or not is_manager(message.from_user.id, settings):
        return

    await message.reply_text("РЎР»СѓС€Р°СЋ РіРѕР»РѕСЃРѕРІРѕРµ Рё РїСЂРµРІСЂР°С‰Р°СЋ РµРіРѕ РІ Р·Р°РґР°С‡Сѓ...")

    try:
        transcript = await transcribe_telegram_voice(message)
    except Exception as error:
        logging.exception("Voice transcription failed")
        await message.reply_text(
            "РќРµ РїРѕР»СѓС‡РёР»РѕСЃСЊ СЂР°СЃС€РёС„СЂРѕРІР°С‚СЊ РіРѕР»РѕСЃРѕРІРѕРµ.\n\n"
            f"РљРѕСЂРѕС‚РєР°СЏ РїСЂРёС‡РёРЅР°: {safe_error_text(error)}\n\n"
            "РџСЂРѕРІРµСЂСЊС‚Рµ YANDEX_API_KEY Рё Р±Р°Р»Р°РЅСЃ Yandex Cloud."
        )
        return

    transcript = transcript.strip()
    if not transcript:
        await message.reply_text("Р“РѕР»РѕСЃРѕРІРѕРµ СЂР°СЃРїРѕР·РЅР°РЅРѕ РїСѓСЃС‚С‹Рј. РџРѕРїСЂРѕР±СѓР№С‚Рµ Р·Р°РїРёСЃР°С‚СЊ РµС‰С‘ СЂР°Р·.")
        return

    await create_task_from_text(update, context, transcript, source_chat_id=message.chat_id)


async def transcribe_telegram_voice(message: Message) -> str:
    telegram_file = await message.voice.get_file()

    with tempfile.TemporaryDirectory() as temporary_directory:
        audio_path = Path(temporary_directory) / "voice.ogg"
        await telegram_file.download_to_drive(custom_path=audio_path)

        return await asyncio.to_thread(transcribe_audio_file, audio_path)


def transcribe_audio_file(audio_path: Path) -> str:
    provider = os.getenv("VOICE_TRANSCRIBER", "yandex").strip().lower()
    if provider == "openai":
        return transcribe_with_openai(audio_path)
    return transcribe_with_yandex(audio_path)


def get_yandex_api_key() -> str:
    return (
        os.getenv("YANDEX_API_KEY", "").strip()
        or os.getenv("YANDEX_SPEECHKIT_API_KEY", "").strip()
    )


def transcribe_with_yandex(audio_path: Path) -> str:
    api_key = get_yandex_api_key()
    if not api_key:
        raise RuntimeError("YANDEX_API_KEY РЅРµ РЅР°Р№РґРµРЅ")

    audio_content = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    headers = {"Authorization": f"Api-Key {api_key}"}
    payload = {
        "content": audio_content,
        "recognitionModel": {
            "model": os.getenv("YANDEX_STT_MODEL", "general"),
            "audioFormat": {
                "containerAudio": {
                    "containerAudioType": "OGG_OPUS",
                },
            },
            "languageRestriction": {
                "restrictionType": "WHITELIST",
                "languageCode": [os.getenv("YANDEX_STT_LANGUAGE", "ru-RU")],
            },
            "textNormalization": {
                "textNormalization": "TEXT_NORMALIZATION_ENABLED",
                "profanityFilter": False,
                "literatureText": False,
            },
        },
    }

    response = requests.post(
        "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync",
        headers=headers,
        json=payload,
        timeout=30,
    )
    ensure_success(response, "Yandex recognizeFileAsync")
    operation = response.json()
    operation_id = operation.get("id")
    if not operation_id:
        raise RuntimeError("Yandex РЅРµ РІРµСЂРЅСѓР» operation id")

    wait_for_yandex_operation(operation_id, headers)
    recognition = requests.get(
        "https://stt.api.cloud.yandex.net/stt/v3/getRecognition",
        headers=headers,
        params={"operationId": operation_id},
        timeout=30,
    )
    ensure_success(recognition, "Yandex getRecognition")
    return extract_yandex_text(recognition.text)


def wait_for_yandex_operation(operation_id: str, headers: dict[str, str]) -> None:
    timeout_seconds = int(os.getenv("YANDEX_STT_TIMEOUT_SECONDS", "120"))
    poll_seconds = float(os.getenv("YANDEX_STT_POLL_SECONDS", "2"))
    deadline = time_module.monotonic() + timeout_seconds

    while time_module.monotonic() < deadline:
        response = requests.get(
            f"https://operation.api.cloud.yandex.net/operations/{operation_id}",
            headers=headers,
            timeout=15,
        )
        ensure_success(response, "Yandex operation status")
        operation = response.json()
        if operation.get("done"):
            if "error" in operation:
                message = operation["error"].get("message", "РѕС€РёР±РєР° СЂР°СЃРїРѕР·РЅР°РІР°РЅРёСЏ Yandex")
                raise RuntimeError(message)
            return
        time_module.sleep(poll_seconds)

    raise RuntimeError("Yandex РЅРµ СѓСЃРїРµР» СЂР°СЃС€РёС„СЂРѕРІР°С‚СЊ РіРѕР»РѕСЃРѕРІРѕРµ Р·Р° РѕС‚РІРµРґРµРЅРЅРѕРµ РІСЂРµРјСЏ")


def extract_yandex_text(raw_text: str) -> str:
    import json

    texts: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"text", "normalizedText"} and isinstance(child, str):
                    texts.append(child)
                else:
                    collect(child)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for line in raw_text.splitlines() or [raw_text]:
        line = line.strip()
        if not line:
            continue
        try:
            collect(json.loads(line))
        except ValueError:
            if len(line) < 1000:
                texts.append(line)

    unique_texts = list(dict.fromkeys(text.strip() for text in texts if text.strip()))
    return " ".join(unique_texts).strip()


def ensure_success(response: requests.Response, service_name: str) -> None:
    if response.ok:
        return
    text = response.text.strip().replace("\n", " ")
    raise RuntimeError(f"{service_name}: HTTP {response.status_code} {text[:250]}")


def transcribe_with_openai(audio_path: Path) -> str:
    client = OpenAI()
    try:
        with audio_path.open("rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_file,
                response_format="text",
                prompt="Р­С‚Рѕ РіРѕР»РѕСЃРѕРІР°СЏ Р·Р°РґР°С‡Р° СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ РґР»СЏ Р°СЃСЃРёСЃС‚РµРЅС‚Р°. Р Р°СЃС€РёС„СЂСѓР№ РЅР° СЂСѓСЃСЃРєРѕРј СЏР·С‹РєРµ.",
            )
    except Exception:
        with audio_path.open("rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text",
                prompt="Р­С‚Рѕ РіРѕР»РѕСЃРѕРІР°СЏ Р·Р°РґР°С‡Р° СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ РґР»СЏ Р°СЃСЃРёСЃС‚РµРЅС‚Р°. Р Р°СЃС€РёС„СЂСѓР№ РЅР° СЂСѓСЃСЃРєРѕРј СЏР·С‹РєРµ.",
            )
    return str(transcription)


def safe_error_text(error: Exception) -> str:
    text = str(error).strip().replace("\n", " ")
    if not text:
        return error.__class__.__name__
    if "sk-" in text:
        return "РѕС€РёР±РєР° OpenAI API, РєР»СЋС‡ СЃРєСЂС‹С‚"
    if "Api-Key" in text or "YANDEX_API_KEY" in text:
        return text.replace(os.getenv("YANDEX_API_KEY", ""), "РєР»СЋС‡ СЃРєСЂС‹С‚")
    return text[:300]


async def submit_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await submit_task_from_message(update, context)


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    await update.message.reply_text(
        format_task_list(db.list_tasks(), "РђРєС‚РёРІРЅС‹Рµ Р·Р°РґР°С‡Рё"),
        reply_markup=main_menu(update.effective_user.id, settings),
    )


async def waiting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    user_id = update.effective_user.id
    if is_manager(user_id, settings):
        statuses = MANAGER_PENDING_STATUSES
        await show_status_picker(update, db, statuses, "Р–РґСѓС‚ СЂРµС€РµРЅРёСЏ", "manager_waiting")
        return
    await show_status_picker(update, db, MANAGER_PENDING_STATUSES, "Р–РґСѓС‚ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ", "assistant_waiting")


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    await show_status_picker(update, db, tuple(STATUS_BY_KEY.values()), "РЎРІРѕРґРєР° РїРѕ Р·Р°РґР°С‡Р°Рј", "summary")


async def done_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: TaskDatabase = context.application.bot_data["db"]
    await show_task_list(update, db.list_tasks((STATUS_DONE,)), "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°С‡Рё")


async def show_status_picker(
    update: Update,
    db: TaskDatabase,
    statuses: tuple[str, ...],
    title: str,
    source: str,
) -> None:
    rows = []
    include_closed = source == "summary"
    for status in statuses:
        tasks = tasks_for_status(db, status, include_closed=include_closed)
        if tasks:
            rows.append([
                InlineKeyboardButton(
                    f"{status_label(status)}: {len(tasks)}",
                    callback_data=f"summary_status:{source}:{status}",
                )
            ])

    if not rows:
        await update.message.reply_text(f"{title}: Р·Р°РґР°С‡ РЅРµС‚.")
        return

    rows.append([InlineKeyboardButton(BACK_BUTTON, callback_data="back:menu")])
    await update.message.reply_text(
        f"{title}. Р’С‹Р±РµСЂРёС‚Рµ СЃС‚Р°С‚СѓСЃ:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def edit_status_picker(
    query,
    db: TaskDatabase,
    statuses: tuple[str, ...],
    title: str,
    source: str,
) -> None:
    rows = []
    include_closed = source == "summary"
    for status in statuses:
        tasks = tasks_for_status(db, status, include_closed=include_closed)
        if tasks:
            rows.append([
                InlineKeyboardButton(
                    f"{status_label(status)}: {len(tasks)}",
                    callback_data=f"summary_status:{source}:{status}",
                )
            ])

    if not rows:
        await query.edit_message_text(f"{title}: Р·Р°РґР°С‡ РЅРµС‚.")
        return

    rows.append([InlineKeyboardButton(BACK_BUTTON, callback_data="back:menu")])
    await query.edit_message_text(
        f"{title}. Р’С‹Р±РµСЂРёС‚Рµ СЃС‚Р°С‚СѓСЃ:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_task_list(update: Update, tasks: list[Task], title: str) -> None:
    if not tasks:
        await update.message.reply_text(f"{title}: Р·Р°РґР°С‡ РЅРµС‚.")
        return

    rows = [
        [InlineKeyboardButton(short_task_button(task), callback_data=f"task_card:menu:{task.id}")]
        for task in tasks[:40]
    ]
    rows.append([InlineKeyboardButton(BACK_BUTTON, callback_data="back:menu")])
    await update.message.reply_text(title, reply_markup=InlineKeyboardMarkup(rows))


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("РќР°РїРёС€РёС‚Рµ РЅРѕРјРµСЂ Р·Р°РґР°С‡Рё: /done 1")
        return

    try:
        task = db.update_status(int(context.args[0]), STATUS_DONE)
    except KeyError:
        await update.message.reply_text("РўР°РєРѕР№ Р·Р°РґР°С‡Рё РЅРµС‚.")
        return
    await update.message.reply_text(
        f"Р“РѕС‚РѕРІРѕ:\n\n{task_text(task)}",
        reply_markup=main_menu(update.effective_user.id, settings),
    )


async def create_task_from_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    source_chat_id: int | None = None,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    user_id = update.effective_user.id

    if not is_manager(user_id, settings) and not is_assistant(user_id, settings):
        await update.message.reply_text("РЎРѕР·РґР°РІР°С‚СЊ Р·Р°РґР°С‡Рё РјРѕР¶РµС‚ С‚РѕР»СЊРєРѕ Р°СЃСЃРёСЃС‚РµРЅС‚ РёР»Рё СЂСѓРєРѕРІРѕРґРёС‚РµР»СЊ.")
        return

    created_by_role = "manager" if is_manager(user_id, settings) else "assistant"
    assistant_id = pick_assistant_id(settings) if created_by_role == "manager" else user_id
    manager_id = user_id if created_by_role == "manager" else pick_manager_id(settings)

    if assistant_id is None:
        await update.message.reply_text("Р’ РЅР°СЃС‚СЂРѕР№РєР°С… РЅРµ СѓРєР°Р·Р°РЅ ASSISTANT_IDS.")
        return
    if created_by_role == "assistant" and manager_id is None:
        await update.message.reply_text("Р’ РЅР°СЃС‚СЂРѕР№РєР°С… РЅРµ СѓРєР°Р·Р°РЅ MANAGER_IDS.")
        return

    title, deadline, comment = parse_task_input(text)
    if not title:
        await update.message.reply_text(
            "РќР°РїРёС€РёС‚Рµ Р·Р°РґР°С‡Сѓ РѕР±С‹С‡РЅС‹Рј С‚РµРєСЃС‚РѕРј.\n\n"
            "РќР°РїСЂРёРјРµСЂ:\nРџРѕРґРіРѕС‚РѕРІРёС‚СЊ РґРѕРіРѕРІРѕСЂ\n\n"
            "Р•СЃР»Рё С…РѕС‚РёС‚Рµ, РјРѕР¶РЅРѕ РґРѕР±Р°РІРёС‚СЊ РґРµРґР»Р°Р№РЅ Рё РєРѕРјРјРµРЅС‚Р°СЂРёР№:\n"
            "РџРѕРґРіРѕС‚РѕРІРёС‚СЊ РґРѕРіРѕРІРѕСЂ | Р·Р°РІС‚СЂР° 18:00 | РїСЂРѕРІРµСЂРёС‚СЊ СЃСѓРјРјСѓ",
            reply_markup=cancel_menu(),
        )
        return

    task = db.create_task(
        title=title,
        deadline=deadline,
        comment=comment,
        assistant_id=assistant_id,
        manager_id=manager_id,
        created_by_id=user_id,
        created_by_role=created_by_role,
    )

    if update.message.chat.type == "private":
        created_text = "Р—Р°РґР°С‡Р° СЃРѕР·РґР°РЅР°."
        if created_by_role == "manager":
            created_text = "Р—Р°РґР°С‡Р° СЃРѕР·РґР°РЅР° Рё РѕС‚РїСЂР°РІР»РµРЅР° Р°СЃСЃРёСЃС‚РµРЅС‚Сѓ."
        await update.message.reply_text(
            f"{created_text}\n\n{task_text(task)}",
            reply_markup=main_menu(user_id, settings),
        )
    else:
        await update.message.reply_text(f"Р—Р°РґР°С‡Р° #{task.id} СЃРѕР·РґР°РЅР° Рё РѕС‚РїСЂР°РІР»РµРЅР° Р°СЃСЃРёСЃС‚РµРЅС‚Сѓ.")

    if created_by_role == "manager":
        await context.bot.send_message(
            chat_id=assistant_id,
            text="РќРѕРІР°СЏ Р·Р°РґР°С‡Р° РѕС‚ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ. Р’С‹Р±РµСЂРёС‚Рµ СЃС‚Р°С‚СѓСЃ Рё Р·Р°С‚РµРј РЅР°РїРёС€РёС‚Рµ РєРѕРјРјРµРЅС‚Р°СЂРёР№:\n\n"
            f"{task_text(task)}",
            reply_markup=assistant_status_keyboard(task.id),
        )


def parse_task_input(text: str) -> tuple[str, str, str]:
    text = text.strip()
    if not text:
        return "", "", ""

    if "|" not in text:
        return text, "", ""

    parts = [part.strip() for part in text.split("|")]
    title = parts[0] if parts else ""
    deadline = parts[1] if len(parts) > 1 else ""
    comment = parts[2] if len(parts) > 2 else ""
    return title, deadline, comment


async def submit_task_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    user_id = update.effective_user.id

    if not is_assistant(user_id, settings):
        await update.message.reply_text("РЎРґР°РІР°С‚СЊ СЂРµР·СѓР»СЊС‚Р°С‚ РјРѕР¶РµС‚ С‚РѕР»СЊРєРѕ Р°СЃСЃРёСЃС‚РµРЅС‚.")
        return

    selected_task_id = context.user_data.get("submit_task_id")
    parsed = parse_submit_message(update.message, selected_task_id=selected_task_id)
    if parsed is None:
        await update.message.reply_text(
            "РЎРЅР°С‡Р°Р»Р° РІС‹Р±РµСЂРёС‚Рµ Р·Р°РґР°С‡Сѓ РёР· СЃРїРёСЃРєР°, Р·Р°С‚РµРј РѕС‚РїСЂР°РІСЊС‚Рµ СЂРµР·СѓР»СЊС‚Р°С‚ С‚РµРєСЃС‚РѕРј, С„РѕС‚Рѕ РёР»Рё С„Р°Р№Р»РѕРј.",
            reply_markup=cancel_menu(),
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
        await update.message.reply_text("РўР°РєРѕР№ Р·Р°РґР°С‡Рё РЅРµС‚ РёР»Рё Сѓ РЅРµС‘ РЅРµ СѓРєР°Р·Р°РЅ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЊ.")
        return

    await send_result_to_manager(context, manager_id, task)
    context.user_data.pop("submit_task_id", None)
    await update.message.reply_text(
        f"Р РµР·СѓР»СЊС‚Р°С‚ РѕС‚РїСЂР°РІР»РµРЅ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЋ:\n\n{task_text(task)}",
        reply_markup=main_menu(user_id, settings),
    )


def parse_submit_message(message: Message, selected_task_id: int | None = None) -> tuple[int, str, str, str, str] | None:
    text = message.caption or message.text or ""
    text = text.removeprefix("/submit").strip()
    parts = [part.strip() for part in text.split("|", 1)]

    if selected_task_id is not None:
        task_id = int(selected_task_id)
        solution_text = text
    else:
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
    elif message.voice:
        file_id = message.voice.file_id
        file_name = "voice"
        file_type = "voice"
    elif message.audio:
        file_id = message.audio.file_id
        file_name = message.audio.file_name or "audio"
        file_type = "audio"

    if not solution_text and not file_id:
        return None

    return task_id, solution_text, file_id, file_name, file_type


async def send_result_to_manager(context: ContextTypes.DEFAULT_TYPE, manager_id: int, task: Task) -> None:
    text = "РђСЃСЃРёСЃС‚РµРЅС‚ СЃРґР°Р» СЂРµР·СѓР»СЊС‚Р°С‚. Р’С‹Р±РµСЂРёС‚Рµ СЂРµС€РµРЅРёРµ:\n\n" f"{task_text(task)}"

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

    if task.solution_file_id and task.solution_file_type == "voice":
        await context.bot.send_voice(
            chat_id=manager_id,
            voice=task.solution_file_id,
            caption=text,
            reply_markup=manager_decision_keyboard(task.id),
        )
        return

    if task.solution_file_id and task.solution_file_type == "audio":
        await context.bot.send_audio(
            chat_id=manager_id,
            audio=task.solution_file_id,
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

    if action_group == "pick_submit":
        await handle_pick_submit_button(update, context, settings, parts)
        return

    if action_group == "back":
        await handle_back_button(update, context, settings, parts)
        return

    if action_group == "summary_status":
        await handle_summary_status_button(update, context, parts)
        return

    if action_group == "task_card":
        await handle_task_card_button(update, context, parts)
        return

    if action_group == "change_status":
        await handle_change_status_button(update, context, settings, parts)
        return

    if action_group == "assistant_status":
        await handle_assistant_status_button(update, context, settings, parts)
        return

    if action_group == "manager_decision":
        await handle_manager_decision_button(update, context, settings, parts)
        return

    if action_group == "manager_comment":
        await handle_manager_comment_button(update, context, settings, parts)
        return


async def handle_pick_submit_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    parts: list[str],
) -> None:
    query = update.callback_query
    if not is_assistant(query.from_user.id, settings):
        await query.edit_message_text("Р’С‹Р±РёСЂР°С‚СЊ Р·Р°РґР°С‡Сѓ РґР»СЏ СЃРґР°С‡Рё РјРѕР¶РµС‚ С‚РѕР»СЊРєРѕ Р°СЃСЃРёСЃС‚РµРЅС‚.")
        return

    task_id = int(parts[1])
    context.user_data["state"] = "submit_result"
    context.user_data["submit_task_id"] = task_id
    await query.edit_message_text(
        f"Р’С‹Р±СЂР°РЅР° Р·Р°РґР°С‡Р° #{task_id}.\n\n"
        "РўРµРїРµСЂСЊ РѕС‚РїСЂР°РІСЊС‚Рµ СЂРµР·СѓР»СЊС‚Р°С‚ С‚РµРєСЃС‚РѕРј, С„РѕС‚Рѕ РёР»Рё С„Р°Р№Р»РѕРј.\n"
        "Р•СЃР»Рё РѕС‚РїСЂР°РІР»СЏРµС‚Рµ С„Р°Р№Р» РёР»Рё С„РѕС‚Рѕ, РґРѕР±Р°РІСЊС‚Рµ РїРѕРґРїРёСЃСЊ СЃ РєРѕРјРјРµРЅС‚Р°СЂРёРµРј."
    )


async def handle_back_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    parts: list[str],
) -> None:
    query = update.callback_query
    db: TaskDatabase = context.application.bot_data["db"]
    target = parts[1] if len(parts) > 1 else "menu"

    if target == "manager_waiting":
        statuses = MANAGER_PENDING_STATUSES
        await edit_status_picker(query, db, statuses, "Р–РґСѓС‚ СЂРµС€РµРЅРёСЏ", "manager_waiting")
        return
    if target == "assistant_waiting":
        await edit_status_picker(query, db, MANAGER_PENDING_STATUSES, "Р–РґСѓС‚ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ", "assistant_waiting")
        return
    if target == "summary":
        await edit_status_picker(query, db, tuple(STATUS_BY_KEY.values()), "РЎРІРѕРґРєР° РїРѕ Р·Р°РґР°С‡Р°Рј", "summary")
        return

    context.user_data.clear()
    await query.edit_message_text(
        "Р’РµСЂРЅСѓР»РёСЃСЊ РІ РіР»Р°РІРЅРѕРµ РјРµРЅСЋ. РСЃРїРѕР»СЊР·СѓР№С‚Рµ РєРЅРѕРїРєРё РІРЅРёР·Сѓ СЌРєСЂР°РЅР°.",
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Р“Р»Р°РІРЅРѕРµ РјРµРЅСЋ",
        reply_markup=main_menu(query.from_user.id, settings),
    )


async def handle_summary_status_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    db: TaskDatabase = context.application.bot_data["db"]
    source = parts[1] if len(parts) > 2 else "summary"
    status = ":".join(parts[2:]) if len(parts) > 2 else ":".join(parts[1:])
    tasks = tasks_for_status(db, status, include_closed=source == "summary")

    if not tasks:
        await update.callback_query.edit_message_text(f"Р’ СЃС‚Р°С‚СѓСЃРµ В«{status}В» Р·Р°РґР°С‡ РЅРµС‚.")
        return

    rows = [
        [InlineKeyboardButton(short_task_button(task), callback_data=f"task_card:{source}:{task.id}")]
        for task in tasks[:30]
    ]
    rows.append([InlineKeyboardButton(BACK_BUTTON, callback_data=f"back:{source}")])
    await update.callback_query.edit_message_text(
        f"Р—Р°РґР°С‡Рё РІ СЃС‚Р°С‚СѓСЃРµ В«{status}В»:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def handle_task_card_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    user_id = update.callback_query.from_user.id
    if len(parts) >= 3:
        source = parts[1]
        task_id = int(parts[2])
    else:
        source = "menu"
        task_id = int(parts[1])

    try:
        task = db.get_task(task_id)
    except KeyError:
        await update.callback_query.edit_message_text("РўР°РєРѕР№ Р·Р°РґР°С‡Рё РЅРµС‚.")
        return

    rows = []
    if is_assistant(user_id, settings):
        rows.extend([
            [InlineKeyboardButton("РЎРґР°С‚СЊ СЂРµР·СѓР»СЊС‚Р°С‚ РїРѕ СЌС‚РѕР№ Р·Р°РґР°С‡Рµ", callback_data=f"pick_submit:{task.id}")],
            [InlineKeyboardButton("РР·РјРµРЅРёС‚СЊ СЃС‚Р°С‚СѓСЃ", callback_data=f"change_status:{task.id}")],
        ])
    if is_manager(user_id, settings) and task.status != STATUS_DONE:
        rows.append([InlineKeyboardButton("Р”Р°С‚СЊ СЂРµС€РµРЅРёРµ/РєРѕРјРјРµРЅС‚Р°СЂРёР№", callback_data=f"manager_decision:comment:{task.id}")])
    rows.append([InlineKeyboardButton(BACK_BUTTON, callback_data=f"back:{source}")])
    await update.callback_query.edit_message_text(
        task_text(task),
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
    )


async def handle_change_status_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    parts: list[str],
) -> None:
    query = update.callback_query
    if not is_assistant(query.from_user.id, settings):
        await query.edit_message_text("РњРµРЅСЏС‚СЊ СЃС‚Р°С‚СѓСЃ РјРѕР¶РµС‚ С‚РѕР»СЊРєРѕ Р°СЃСЃРёСЃС‚РµРЅС‚.")
        return

    task_id = int(parts[1])
    await query.edit_message_text(
        f"Р’С‹Р±РµСЂРёС‚Рµ РЅРѕРІС‹Р№ СЃС‚Р°С‚СѓСЃ РґР»СЏ Р·Р°РґР°С‡Рё #{task_id}:",
        reply_markup=assistant_status_keyboard(task_id),
    )


async def handle_assistant_status_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    parts: list[str],
) -> None:
    if not is_assistant(update.callback_query.from_user.id, settings):
        await update.callback_query.edit_message_text("Р­С‚Рё РєРЅРѕРїРєРё РґРѕСЃС‚СѓРїРЅС‹ С‚РѕР»СЊРєРѕ Р°СЃСЃРёСЃС‚РµРЅС‚Сѓ.")
        return

    _, status_key, task_id_text = parts
    status = STATUS_BY_KEY[status_key]
    task_id = int(task_id_text)
    pending = context.application.bot_data.setdefault("pending_assistant_comments", {})
    pending[update.callback_query.from_user.id] = (task_id, status)

    await update.callback_query.edit_message_text(
        f"Р’С‹ РІС‹Р±СЂР°Р»Рё СЃС‚Р°С‚СѓСЃ: {status}\n\n"
        "РўРµРїРµСЂСЊ РЅР°РїРёС€РёС‚Рµ РєРѕРјРјРµРЅС‚Р°СЂРёР№ Рє СЂСѓРєРѕРІРѕРґРёС‚РµР»СЋ РѕРґРЅРёРј СЃРѕРѕР±С‰РµРЅРёРµРј.\n"
        "Р•СЃР»Рё РєРѕРјРјРµРЅС‚Р°СЂРёР№ РЅРµ РЅСѓР¶РµРЅ, РЅР°РїРёС€РёС‚Рµ: -"
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
        await query.edit_message_text("Р­С‚Рё РєРЅРѕРїРєРё РґРѕСЃС‚СѓРїРЅС‹ С‚РѕР»СЊРєРѕ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЋ.")
        return

    _, decision, task_id_text = parts
    task_id = int(task_id_text)

    status_by_decision = {
        "comment": STATUS_NEEDS_INPUT,
        "done": STATUS_DONE,
        "revision": STATUS_NEEDS_INPUT,
        "cancelled": STATUS_CANCELLED,
        "postponed": STATUS_POSTPONED,
    }
    status = status_by_decision[decision]
    pending = context.application.bot_data.setdefault("pending_manager_feedback", {})
    pending[query.from_user.id] = (task_id, status)

    await query.edit_message_text(
        f"Вы выбрали: {MANAGER_DECISION_LABELS.get(decision, status)}\n\n"
        "Теперь напишите комментарий для ассистента.\n"
        "Если комментарий не нужен, напишите: -"
    )

async def handle_manager_comment_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    parts: list[str],
) -> None:
    query = update.callback_query
    db: TaskDatabase = context.application.bot_data["db"]

    if not is_manager(query.from_user.id, settings):
        await query.edit_message_text("РљРѕРјРјРµРЅС‚РёСЂРѕРІР°С‚СЊ Р·Р°РґР°С‡Сѓ РјРѕР¶РµС‚ С‚РѕР»СЊРєРѕ СЂСѓРєРѕРІРѕРґРёС‚РµР»СЊ.")
        return

    task_id = int(parts[1])
    try:
        task = db.get_task(task_id)
    except KeyError:
        await query.edit_message_text("РўР°РєРѕР№ Р·Р°РґР°С‡Рё РЅРµС‚.")
        return

    pending = context.application.bot_data.setdefault("pending_manager_feedback", {})
    pending[query.from_user.id] = (task_id, task.status)
    await query.edit_message_text(
        f"РќР°РїРёС€РёС‚Рµ РєРѕРјРјРµРЅС‚Р°СЂРёР№ РґР»СЏ Р°СЃСЃРёСЃС‚РµРЅС‚Р° РїРѕ Р·Р°РґР°С‡Рµ #{task_id}:\n\n{task_text(task)}"
    )


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user_id = update.effective_user.id
    text = update.message.text.strip()
    db: TaskDatabase = context.application.bot_data["db"]

    if text == MENU_CANCEL:
        context.user_data.clear()
        await update.message.reply_text(
            "Р”РµР№СЃС‚РІРёРµ РѕС‚РјРµРЅРµРЅРѕ.",
            reply_markup=main_menu(user_id, settings),
        )
        return

    state = context.user_data.get("state")
    if state == "create_task":
        context.user_data.clear()
        await create_task_from_text(update, context, text)
        return

    if state == "submit_result":
        await submit_task_from_message(update, context)
        context.user_data.clear()
        return

    if text == MENU_NEW_TASK:
        if not is_manager(user_id, settings) and not is_assistant(user_id, settings):
            await update.message.reply_text("Р­С‚Р° РєРЅРѕРїРєР° РґРѕСЃС‚СѓРїРЅР° С‚РѕР»СЊРєРѕ Р°СЃСЃРёСЃС‚РµРЅС‚Сѓ РёР»Рё СЂСѓРєРѕРІРѕРґРёС‚РµР»СЋ.")
            return
        context.user_data["state"] = "create_task"
        await update.message.reply_text(
            "РќР°РїРёС€РёС‚Рµ Р·Р°РґР°С‡Сѓ РѕР±С‹С‡РЅС‹Рј С‚РµРєСЃС‚РѕРј.\n\n"
            "РџСЂРёРјРµСЂ:\nРџРѕРґРіРѕС‚РѕРІРёС‚СЊ РґРѕРіРѕРІРѕСЂ\n\n"
            "Р•СЃР»Рё РЅСѓР¶РµРЅ РґРµРґР»Р°Р№РЅ РёР»Рё РєРѕРјРјРµРЅС‚Р°СЂРёР№, РјРѕР¶РЅРѕ С‚Р°Рє:\n"
            "РџРѕРґРіРѕС‚РѕРІРёС‚СЊ РґРѕРіРѕРІРѕСЂ | Р·Р°РІС‚СЂР° 18:00 | РїСЂРѕРІРµСЂРёС‚СЊ СЃСѓРјРјСѓ",
            reply_markup=cancel_menu(),
        )
        return

    if text == MENU_SUBMIT:
        if not is_assistant(user_id, settings):
            await update.message.reply_text("Р­С‚Р° РєРЅРѕРїРєР° РґРѕСЃС‚СѓРїРЅР° С‚РѕР»СЊРєРѕ Р°СЃСЃРёСЃС‚РµРЅС‚Сѓ.")
            return
        await show_submit_task_picker(update, context)
        return

    if text == MENU_LIST:
        await list_tasks(update, context)
        return

    if text in {MENU_WAITING, MENU_MANAGER_WAITING}:
        await waiting(update, context)
        return

    if text in {MENU_SUMMARY, MENU_MANAGER_SUMMARY}:
        await summary(update, context)
        return

    if text == MENU_DONE_TASKS:
        await done_tasks(update, context)
        return

    if text == MENU_WHOAMI:
        await whoami(update, context)
        return

    pending_assistant = context.application.bot_data.setdefault("pending_assistant_comments", {})
    if user_id in pending_assistant:
        task_id, status = pending_assistant.pop(user_id)
        comment = "" if text == "-" else text
        try:
            task = db.update_status(task_id, status, assistant_comment=comment)
        except KeyError:
            await update.message.reply_text("РўР°РєРѕР№ Р·Р°РґР°С‡Рё РЅРµС‚.")
            return

        await update.message.reply_text(
            f"РЎС‚Р°С‚СѓСЃ СЃРѕС…СЂР°РЅС‘РЅ:\n\n{task_text(task)}",
            reply_markup=main_menu(user_id, settings),
        )
        if task.manager_id:
            await context.bot.send_message(
                chat_id=task.manager_id,
                text=f"РђСЃСЃРёСЃС‚РµРЅС‚ РѕР±РЅРѕРІРёР» СЃС‚Р°С‚СѓСЃ Р·Р°РґР°С‡Рё #{task.id}:\n\n{task_text(task)}",
                reply_markup=manager_feedback_keyboard(task.id),
            )
        return

    pending_manager = context.application.bot_data.setdefault("pending_manager_feedback", {})
    if user_id in pending_manager:
        task_id, status = pending_manager.pop(user_id)
        try:
            task = db.update_status(task_id, status, manager_feedback=text)
        except KeyError:
            await update.message.reply_text("РўР°РєРѕР№ Р·Р°РґР°С‡Рё РЅРµС‚.")
            return

        await update.message.reply_text(
            f"РљРѕРјРјРµРЅС‚Р°СЂРёР№ РѕС‚РїСЂР°РІР»РµРЅ Р°СЃСЃРёСЃС‚РµРЅС‚Сѓ:\n\n{task_text(task)}",
            reply_markup=main_menu(user_id, settings),
        )
        await context.bot.send_message(
            chat_id=task.assistant_id,
            text=f"Р СѓРєРѕРІРѕРґРёС‚РµР»СЊ РґР°Р» РѕР±СЂР°С‚РЅСѓСЋ СЃРІСЏР·СЊ РїРѕ Р·Р°РґР°С‡Рµ #{task.id}:\n\n{task_text(task)}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("РР·РјРµРЅРёС‚СЊ СЃС‚Р°С‚СѓСЃ", callback_data=f"change_status:{task.id}")],
                    [InlineKeyboardButton("РЎРґР°С‚СЊ СЂРµР·СѓР»СЊС‚Р°С‚", callback_data=f"pick_submit:{task.id}")],
                ]
            ),
        )
        return

    await update.message.reply_text(
        "Р’С‹Р±РµСЂРёС‚Рµ РґРµР№СЃС‚РІРёРµ РєРЅРѕРїРєРѕР№ РІРЅРёР·Сѓ РёР»Рё РЅР°РїРёС€РёС‚Рµ /start.",
        reply_markup=main_menu(user_id, settings),
    )


async def show_submit_task_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: TaskDatabase = context.application.bot_data["db"]
    tasks = db.list_tasks()
    if not tasks:
        await update.message.reply_text("РђРєС‚РёРІРЅС‹С… Р·Р°РґР°С‡ РїРѕРєР° РЅРµС‚.")
        return

    rows = []
    for task in tasks[:20]:
        rows.append([InlineKeyboardButton(f"#{task.id} {task.title[:40]}", callback_data=f"pick_submit:{task.id}")])
    rows.append([InlineKeyboardButton(BACK_BUTTON, callback_data="back:menu")])

    await update.message.reply_text(
        "Р’С‹Р±РµСЂРёС‚Рµ Р·Р°РґР°С‡Сѓ, РїРѕ РєРѕС‚РѕСЂРѕР№ С…РѕС‚РёС‚Рµ СЃРґР°С‚СЊ СЂРµР·СѓР»СЊС‚Р°С‚:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    user_id = update.effective_user.id
    if context.user_data.get("state") != "submit_result":
        return

    await submit_task_from_message(update, context)
    context.user_data.clear()
    await update.message.reply_text(reply_markup=main_menu(user_id, settings), text="РњРµРЅСЋ РІРѕР·РІСЂР°С‰РµРЅРѕ.")


async def remind_managers(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]

    for manager_id in settings.manager_ids:
        sent_any = False
        for status in MANAGER_PENDING_STATUSES:
            tasks = [
                task
                for task in db.list_tasks((status,))
                if task.manager_id == manager_id or task.created_by_id == manager_id
            ]
            if not tasks:
                continue

            sent_any = True
            rows = [
                [InlineKeyboardButton(short_task_button(task), callback_data=f"task_card:manager_waiting:{task.id}")]
                for task in tasks[:30]
            ]
            await context.bot.send_message(
                chat_id=manager_id,
                text=f"РќР°РїРѕРјРёРЅР°РЅРёРµ РЅР° 10:00 РњРЎРљ\n{status_label(status)}: {len(tasks)}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            for task in tasks:
                db.mark_reminded(task.id)

        if not sent_any:
            await context.bot.send_message(
                chat_id=manager_id,
                text="РќР°РїРѕРјРёРЅР°РЅРёРµ РЅР° 10:00 РњРЎРљ: Р·Р°РґР°С‡, РѕР¶РёРґР°СЋС‰РёС… СЂРµС€РµРЅРёСЏ, РЅРµС‚.",
            )


async def morning_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: TaskDatabase = context.application.bot_data["db"]
    text = format_task_list(db.list_tasks()[:10], "РЈС‚СЂРµРЅРЅРёР№ РґР°Р№РґР¶РµСЃС‚")
    for manager_id in settings.manager_ids:
        await context.bot.send_message(chat_id=manager_id, text=text)


def format_task_list(tasks: list[Task], title: str) -> str:
    if not tasks:
        return f"{title}: РїСѓСЃС‚Рѕ."
    return f"{title}:\n\n" + "\n\n".join(task_text(task) for task in tasks)


def build_application(settings: Settings | None = None, db: TaskDatabase | None = None) -> Application:
    settings = settings or load_settings()
    db = db or TaskDatabase(settings.database_path)

    application = Application.builder().token(settings.bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("check_voice", check_voice))
    application.add_handler(CommandHandler("check_openai", check_voice))
    application.add_handler(CommandHandler("new", new_task))
    application.add_handler(CommandHandler("submit", submit_task))
    application.add_handler(CommandHandler("list", list_tasks))
    application.add_handler(CommandHandler("waiting", waiting))
    application.add_handler(CommandHandler("summary", summary))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(MessageHandler(filters.CaptionRegex(r"^/submit"), submit_task))
    application.add_handler(MessageHandler((filters.Document.ALL | filters.PHOTO) & filters.CaptionRegex(r"^\d+"), handle_attachment))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.VOICE, group_voice_task_message))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_task_message))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.Document.ALL | filters.PHOTO | filters.VOICE | filters.AUDIO), handle_attachment))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))

    if application.job_queue:
        application.job_queue.run_daily(
            remind_managers,
            time=time(hour=10, minute=0, tzinfo=MSK),
        )
        application.job_queue.run_daily(
            morning_digest,
            time=time(hour=settings.digest_hour, minute=settings.digest_minute, tzinfo=MSK),
        )

    return application


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()




