import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MOD_CHAT_ID = int(os.getenv("TELEGRAM_MOD_CHAT_ID", "0").strip() or "0")
DB_PATH = os.getenv("DB_PATH", "reports.db").strip()
SERVER_NAME = os.getenv("SERVER_NAME", "Mambo City").strip() or "Mambo City"
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "polling").strip().lower() or "polling"
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram/webhook").strip().strip("/")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
RULES_TEXT = os.getenv(
    "RULES_TEXT",
    "Правила сервера уточни у администрации или в закрепе канала.",
).strip()
SUPPORT_TEXT = os.getenv(
    "SUPPORT_TEXT",
    "Если нужна помощь срочно, напиши модератору в этот чат.",
).strip()

try:
    PORT = int(os.getenv("PORT", "10000").strip() or "10000")
except ValueError:
    PORT = 10000

raw_mod_ids = os.getenv("TELEGRAM_MOD_USER_IDS", "").strip()
MOD_USER_IDS: set[int] = set()
if raw_mod_ids:
    for value in raw_mod_ids.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            MOD_USER_IDS.add(int(value))
        except ValueError:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mambo-city-telegram-bot")


STATE_NICK, STATE_REASON, STATE_TIME, STATE_DETAILS, STATE_CONTACT = range(5)
STATE_ADD_DETAILS = 100
STATE_MOD_REPLY_TEXT = 200

TICKET_TYPE_COMPLAINT = "complaint"
TICKET_TYPE_EVENT = "event"
TICKET_TYPE_OTHER = "other"

CALLBACK_START_PREFIX = "ticket_start:"
CALLBACK_USER_ADD_PREFIX = "user_add:"
CALLBACK_MOD_TAKE_PREFIX = "mod_take:"
CALLBACK_MOD_CLOSE_PREFIX = "mod_close:"
CALLBACK_MOD_REPLY_PREFIX = "mod_reply:"

CALLBACK_MENU_MY_REPORTS = "menu_myreports"
CALLBACK_MENU_RULES = "menu_rules"
CALLBACK_MENU_SUPPORT = "menu_support"

MENU_COMPLAINT = "Жалоба"
MENU_EVENT = "Ивент"
MENU_OTHER = "Другое"
MENU_MY_REPORTS = "Мои заявки"
MENU_RULES = "Правила"
MENU_SUPPORT = "Поддержка"
MENU_CANCEL = "Отмена"

TICKET_TYPE_META = {
    TICKET_TYPE_COMPLAINT: {
        "label": "Жалоба",
        "reason_prompt": "Шаг 2/5. Причина жалобы (кратко):",
    },
    TICKET_TYPE_EVENT: {
        "label": "Участие в ивенте",
        "reason_prompt": "Шаг 2/5. На какой ивент хочешь заявку:",
    },
    TICKET_TYPE_OTHER: {
        "label": "Другое обращение",
        "reason_prompt": "Шаг 2/5. Тема обращения:",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_human_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing_columns = {row[1] for row in rows}
    if column not in existing_columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def normalize_ticket_type(raw_value: str) -> str:
    if raw_value in TICKET_TYPE_META:
        return raw_value
    return TICKET_TYPE_OTHER


def ticket_label(ticket_type: str) -> str:
    return TICKET_TYPE_META[normalize_ticket_type(ticket_type)]["label"]


def ticket_reason_prompt(ticket_type: str) -> str:
    return TICKET_TYPE_META[normalize_ticket_type(ticket_type)]["reason_prompt"]


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tg_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT NOT NULL,
                telegram_name TEXT NOT NULL,
                minecraft_nick TEXT NOT NULL,
                reason TEXT NOT NULL,
                approx_time TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                contact TEXT NOT NULL DEFAULT '',
                ticket_type TEXT NOT NULL DEFAULT 'complaint',
                status TEXT NOT NULL DEFAULT 'open',
                assigned_to INTEGER,
                assigned_at TEXT,
                closed_by INTEGER,
                closed_at TEXT,
                close_note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        ensure_column(conn, "tg_reports", "details", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "tg_reports", "contact", "TEXT NOT NULL DEFAULT ''")
        ensure_column(
            conn,
            "tg_reports",
            "ticket_type",
            "TEXT NOT NULL DEFAULT 'complaint'",
        )
        ensure_column(
            conn,
            "tg_reports",
            "status",
            "TEXT NOT NULL DEFAULT 'open'",
        )
        ensure_column(conn, "tg_reports", "assigned_to", "INTEGER")
        ensure_column(conn, "tg_reports", "assigned_at", "TEXT")
        ensure_column(conn, "tg_reports", "closed_by", "INTEGER")
        ensure_column(conn, "tg_reports", "closed_at", "TEXT")
        ensure_column(conn, "tg_reports", "close_note", "TEXT NOT NULL DEFAULT ''")
        conn.commit()


def create_report(
    telegram_user_id: int,
    telegram_username: str,
    telegram_name: str,
    ticket_type: str,
    minecraft_nick: str,
    reason: str,
    approx_time: str,
    details: Optional[str],
    contact: Optional[str],
) -> int:
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO tg_reports (
                created_at,
                telegram_user_id,
                telegram_username,
                telegram_name,
                ticket_type,
                minecraft_nick,
                reason,
                approx_time,
                details,
                contact,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                now_iso(),
                telegram_user_id,
                telegram_username,
                telegram_name,
                normalize_ticket_type(ticket_type),
                minecraft_nick,
                reason,
                approx_time,
                details or "",
                contact or "",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_report(report_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM tg_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        return row


def get_reports_for_user(telegram_user_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tg_reports
            WHERE telegram_user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (telegram_user_id, limit),
        ).fetchall()
        return list(rows)


def append_report_details(report_id: int, extra_text: str) -> bool:
    row = get_report(report_id)
    if row is None:
        return False
    base = (row["details"] or "").strip()
    extra_line = f"[Дополнение {now_human_utc()}] {extra_text.strip()}"
    combined = f"{base}\n{extra_line}".strip() if base else extra_line
    with db_connect() as conn:
        cur = conn.execute(
            "UPDATE tg_reports SET details = ? WHERE id = ?",
            (combined, report_id),
        )
        conn.commit()
        return cur.rowcount > 0


def mark_report_taken(report_id: int, moderator_id: int) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            """
            UPDATE tg_reports
            SET status = 'in_progress',
                assigned_to = ?,
                assigned_at = ?
            WHERE id = ? AND status != 'closed'
            """,
            (moderator_id, now_iso(), report_id),
        )
        conn.commit()
        return cur.rowcount > 0


def close_report(report_id: int, moderator_id: int, close_note: str = "") -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            """
            UPDATE tg_reports
            SET status = 'closed',
                closed_by = ?,
                closed_at = ?,
                close_note = ?
            WHERE id = ? AND status != 'closed'
            """,
            (moderator_id, now_iso(), close_note.strip(), report_id),
        )
        conn.commit()
        return cur.rowcount > 0


def is_moderator(user_id: int) -> bool:
    return user_id in MOD_USER_IDS


def status_label(status: str) -> str:
    return {
        "open": "Открыта",
        "in_progress": "В работе",
        "closed": "Закрыта",
    }.get((status or "").strip(), status or "unknown")


def build_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_COMPLAINT, MENU_EVENT, MENU_OTHER],
            [MENU_MY_REPORTS, MENU_RULES, MENU_SUPPORT],
            [MENU_CANCEL],
        ],
        resize_keyboard=True,
    )


def build_dashboard_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Подать жалобу",
                    callback_data=f"{CALLBACK_START_PREFIX}{TICKET_TYPE_COMPLAINT}",
                ),
                InlineKeyboardButton(
                    "Участие в ивенте",
                    callback_data=f"{CALLBACK_START_PREFIX}{TICKET_TYPE_EVENT}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Другое обращение",
                    callback_data=f"{CALLBACK_START_PREFIX}{TICKET_TYPE_OTHER}",
                ),
                InlineKeyboardButton("Мои заявки", callback_data=CALLBACK_MENU_MY_REPORTS),
            ],
            [
                InlineKeyboardButton("Правила", callback_data=CALLBACK_MENU_RULES),
                InlineKeyboardButton("Поддержка", callback_data=CALLBACK_MENU_SUPPORT),
            ],
        ]
    )


def build_user_report_keyboard(report_id: int, status: str) -> Optional[InlineKeyboardMarkup]:
    if (status or "").strip() == "closed":
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Дополнить заявку",
                    callback_data=f"{CALLBACK_USER_ADD_PREFIX}{report_id}",
                )
            ]
        ]
    )


def build_mod_actions_keyboard(row: sqlite3.Row) -> Optional[InlineKeyboardMarkup]:
    report_id = int(row["id"])
    status = (row["status"] or "").strip()

    if status == "closed":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Ответить игроку",
                        callback_data=f"{CALLBACK_MOD_REPLY_PREFIX}{report_id}",
                    )
                ]
            ]
        )

    if status == "in_progress":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Закрыть",
                        callback_data=f"{CALLBACK_MOD_CLOSE_PREFIX}{report_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Ответить игроку",
                        callback_data=f"{CALLBACK_MOD_REPLY_PREFIX}{report_id}",
                    )
                ],
            ]
        )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Взять в работу",
                    callback_data=f"{CALLBACK_MOD_TAKE_PREFIX}{report_id}",
                ),
                InlineKeyboardButton(
                    "Закрыть",
                    callback_data=f"{CALLBACK_MOD_CLOSE_PREFIX}{report_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Ответить игроку",
                    callback_data=f"{CALLBACK_MOD_REPLY_PREFIX}{report_id}",
                )
            ],
        ]
    )


def format_report_for_mod(row: sqlite3.Row) -> str:
    assignee = row["assigned_to"] if row["assigned_to"] is not None else "-"
    close_note = (row["close_note"] or "").strip() or "-"
    username = row["telegram_username"] or "no_username"
    lines = [
        f"{SERVER_NAME} | Заявка #{row['id']}",
        f"Тип: {ticket_label(row['ticket_type'])}",
        f"Статус: {status_label(row['status'])}",
        f"Ник Minecraft: {row['minecraft_nick']}",
        f"Причина/тема: {row['reason']}",
        f"Время инцидента: {row['approx_time']}",
        f"Детали: {row['details'] or '-'}",
        f"Контакт: {row['contact'] or '-'}",
        f"Ответственный модератор ID: {assignee}",
        f"Комментарий закрытия: {close_note}",
        (
            f"Автор: {row['telegram_name']} "
            f"(@{username}, id={row['telegram_user_id']})"
        ),
    ]
    return "\n".join(lines)


def format_report_for_player(row: sqlite3.Row) -> str:
    return "\n".join(
        [
            f"Заявка #{row['id']} отправлена.",
            f"Тип: {ticket_label(row['ticket_type'])}",
            f"Статус: {status_label(row['status'])}",
            f"Ник: {row['minecraft_nick']}",
            f"Причина/тема: {row['reason']}",
            f"Время инцидента: {row['approx_time']}",
            f"Контакт: {row['contact'] or '-'}",
        ]
    )


def user_reports_text(telegram_user_id: int) -> str:
    rows = get_reports_for_user(telegram_user_id, limit=10)
    if not rows:
        return "У тебя пока нет заявок."
    lines = ["Твои заявки:"]
    for row in rows:
        lines.append(
            f"#{row['id']} | {ticket_label(row['ticket_type'])} | "
            f"{status_label(row['status'])} | {row['reason'][:45]}"
        )
    return "\n".join(lines)


def parse_report_id(data: str, prefix: str) -> Optional[int]:
    if not data.startswith(prefix):
        return None
    raw_id = data[len(prefix) :].strip()
    if not raw_id.isdigit():
        return None
    return int(raw_id)


async def notify_user(application: Application, telegram_user_id: int, text: str) -> None:
    try:
        await application.bot.send_message(chat_id=telegram_user_id, text=text)
    except Exception as exc:
        log.warning("Could not notify user %s: %s", telegram_user_id, exc)


async def send_to_mod_chat(application: Application, row: sqlite3.Row) -> None:
    if MOD_CHAT_ID == 0:
        return
    try:
        await application.bot.send_message(
            chat_id=MOD_CHAT_ID,
            text=format_report_for_mod(row),
            reply_markup=build_mod_actions_keyboard(row),
        )
    except Exception as exc:
        log.warning("Could not send report to moderation chat: %s", exc)


async def send_addition_to_mod_chat(
    application: Application, row: sqlite3.Row, addition_text: str
) -> None:
    if MOD_CHAT_ID == 0:
        return
    try:
        await application.bot.send_message(
            chat_id=MOD_CHAT_ID,
            text=(
                f"Дополнение к заявке #{row['id']}:\n"
                f"{addition_text}\n\n"
                f"{format_report_for_mod(row)}"
            ),
            reply_markup=build_mod_actions_keyboard(row),
        )
    except Exception as exc:
        log.warning("Could not send additional details to moderation chat: %s", exc)


async def refresh_mod_message_if_possible(update: Update, row: sqlite3.Row) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.edit_message_text(
            text=format_report_for_mod(row),
            reply_markup=build_mod_actions_keyboard(row),
        )
    except Exception as exc:
        log.warning("Could not edit moderation message for report %s: %s", row["id"], exc)


def is_skip_value(value: str) -> bool:
    return value.strip().lower() in {"-", "none", "no", "нет", "пропустить"}


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        f"{SERVER_NAME} | Центр заявок\n\n"
        "Выбери действие кнопкой ниже.\n"
        "Форма: 1) ник 2) причина 3) время инцидента 4) детали 5) контакт",
        reply_markup=build_dashboard_inline_keyboard(),
    )
    await msg.reply_text("Быстрое меню:", reply_markup=build_main_keyboard())


async def dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    data = (query.data or "").strip()

    if data == CALLBACK_MENU_MY_REPORTS:
        await query.message.reply_text(
            user_reports_text(user.id),
            reply_markup=build_main_keyboard(),
        )
        return
    if data == CALLBACK_MENU_RULES:
        await query.message.reply_text(RULES_TEXT, reply_markup=build_main_keyboard())
        return
    if data == CALLBACK_MENU_SUPPORT:
        await query.message.reply_text(SUPPORT_TEXT, reply_markup=build_main_keyboard())
        return


async def start_ticket(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_type: str
) -> int:
    msg = update.effective_message
    if msg is None:
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["ticket_type"] = normalize_ticket_type(ticket_type)
    await msg.reply_text(
        f"Создаем обращение: {ticket_label(ticket_type)}\n"
        "Шаг 1/5. Введи ник Minecraft:",
        reply_markup=build_main_keyboard(),
    )
    return STATE_NICK


async def start_ticket_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()
    data = (query.data or "").strip()
    if not data.startswith(CALLBACK_START_PREFIX):
        return ConversationHandler.END
    ticket_type = data[len(CALLBACK_START_PREFIX) :].strip()
    return await start_ticket(update, context, ticket_type)


async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start_ticket(update, context, TICKET_TYPE_COMPLAINT)


async def event_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start_ticket(update, context, TICKET_TYPE_EVENT)


async def other_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start_ticket(update, context, TICKET_TYPE_OTHER)


async def menu_report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await report_start(update, context)


async def menu_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await event_start(update, context)


async def menu_other_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await other_start(update, context)


async def report_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return STATE_NICK
    nick = msg.text.strip()
    if not nick:
        await msg.reply_text("Ник не может быть пустым. Попробуй еще:")
        return STATE_NICK
    if len(nick) > 32:
        await msg.reply_text("Ник слишком длинный (макс. 32). Попробуй еще:")
        return STATE_NICK

    context.user_data["minecraft_nick"] = nick
    ticket_type = normalize_ticket_type(
        context.user_data.get("ticket_type", TICKET_TYPE_COMPLAINT).strip()
    )
    await msg.reply_text(ticket_reason_prompt(ticket_type))
    return STATE_REASON


async def report_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return STATE_REASON
    reason = msg.text.strip()
    if len(reason) < 3:
        await msg.reply_text("Слишком коротко. Напиши хотя бы 3 символа:")
        return STATE_REASON
    context.user_data["reason"] = reason
    await msg.reply_text(
        "Шаг 3/5. Укажи время именно инцидента (когда это случилось):"
    )
    return STATE_TIME


async def report_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return STATE_TIME
    incident_time = msg.text.strip()
    if not incident_time:
        await msg.reply_text("Время инцидента не может быть пустым. Попробуй еще:")
        return STATE_TIME
    context.user_data["approx_time"] = incident_time
    await msg.reply_text("Шаг 4/5. Детали / доказательства (или '-' чтобы пропустить):")
    return STATE_DETAILS


async def report_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return STATE_DETAILS
    details = msg.text.strip()
    if is_skip_value(details):
        details = ""
    context.user_data["details"] = details
    await msg.reply_text(
        "Шаг 5/5. Контакт для ответа (Discord/TG) или '-' чтобы пропустить:"
    )
    return STATE_CONTACT


async def report_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or msg.text is None or user is None:
        return ConversationHandler.END

    contact = msg.text.strip()
    if is_skip_value(contact):
        contact = ""

    report_id = create_report(
        telegram_user_id=user.id,
        telegram_username=user.username or "",
        telegram_name=user.full_name or "",
        ticket_type=context.user_data.get("ticket_type", TICKET_TYPE_COMPLAINT).strip(),
        minecraft_nick=context.user_data.get("minecraft_nick", "").strip(),
        reason=context.user_data.get("reason", "").strip(),
        approx_time=context.user_data.get("approx_time", "").strip(),
        details=context.user_data.get("details", "").strip(),
        contact=contact,
    )

    row = get_report(report_id)
    if row is not None:
        await send_to_mod_chat(context.application, row)
        await msg.reply_text(
            format_report_for_player(row),
            reply_markup=build_user_report_keyboard(report_id, row["status"]),
        )
    else:
        await msg.reply_text(f"Готово. Заявка #{report_id} отправлена.")

    context.user_data.clear()
    return ConversationHandler.END


async def user_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return ConversationHandler.END
    await query.answer()
    report_id = parse_report_id((query.data or "").strip(), CALLBACK_USER_ADD_PREFIX)
    if report_id is None:
        await query.message.reply_text("Не удалось определить заявку.")
        return ConversationHandler.END

    row = get_report(report_id)
    if row is None:
        await query.message.reply_text("Заявка не найдена.")
        return ConversationHandler.END

    if int(row["telegram_user_id"]) != int(user.id):
        await query.message.reply_text("Можно дополнять только свои заявки.")
        return ConversationHandler.END

    if (row["status"] or "").strip() == "closed":
        await query.message.reply_text("Эта заявка уже закрыта.")
        return ConversationHandler.END

    context.user_data["add_report_id"] = report_id
    await query.message.reply_text("Напиши дополнение к заявке одним сообщением:")
    return STATE_ADD_DETAILS


async def user_add_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or msg.text is None or user is None:
        return STATE_ADD_DETAILS

    report_id = int(context.user_data.get("add_report_id", 0) or 0)
    if report_id <= 0:
        await msg.reply_text("Сессия дополнения не найдена. Нажми кнопку снова.")
        return ConversationHandler.END

    addition = msg.text.strip()
    if len(addition) < 2:
        await msg.reply_text("Слишком коротко. Напиши подробнее:")
        return STATE_ADD_DETAILS

    row = get_report(report_id)
    if row is None or int(row["telegram_user_id"]) != int(user.id):
        await msg.reply_text("Не могу обновить эту заявку.")
        context.user_data.pop("add_report_id", None)
        return ConversationHandler.END

    ok = append_report_details(report_id, addition)
    if not ok:
        await msg.reply_text("Не удалось сохранить дополнение.")
        context.user_data.pop("add_report_id", None)
        return ConversationHandler.END

    row = get_report(report_id)
    if row is not None:
        await send_addition_to_mod_chat(context.application, row, addition)

    await msg.reply_text("Дополнение добавлено и отправлено модераторам.")
    context.user_data.pop("add_report_id", None)
    return ConversationHandler.END


async def mod_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    data = (query.data or "").strip()
    if not is_moderator(user.id):
        await query.answer("Нет прав", show_alert=True)
        return

    if data.startswith(CALLBACK_MOD_TAKE_PREFIX):
        report_id = parse_report_id(data, CALLBACK_MOD_TAKE_PREFIX)
        if report_id is None:
            await query.answer("Ошибка ID", show_alert=True)
            return
        ok = mark_report_taken(report_id, user.id)
        if not ok:
            await query.answer("Заявка не найдена или закрыта", show_alert=True)
            return

        row = get_report(report_id)
        if row is not None:
            await refresh_mod_message_if_possible(update, row)
            await notify_user(
                context.application,
                int(row["telegram_user_id"]),
                f"Заявка #{report_id} взята в работу модератором.",
            )
        await query.answer("Заявка взята в работу")
        return

    if data.startswith(CALLBACK_MOD_CLOSE_PREFIX):
        report_id = parse_report_id(data, CALLBACK_MOD_CLOSE_PREFIX)
        if report_id is None:
            await query.answer("Ошибка ID", show_alert=True)
            return
        ok = close_report(report_id, user.id)
        if not ok:
            await query.answer("Заявка не найдена или уже закрыта", show_alert=True)
            return

        row = get_report(report_id)
        if row is not None:
            await refresh_mod_message_if_possible(update, row)
            await notify_user(
                context.application,
                int(row["telegram_user_id"]),
                (
                    f"Заявка #{report_id} закрыта модератором. "
                    "Если есть новые факты, можешь создать новую заявку."
                ),
            )
        await query.answer("Заявка закрыта")
        return


async def mod_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return ConversationHandler.END

    await query.answer()
    if not is_moderator(user.id):
        await query.message.reply_text("Нет прав.")
        return ConversationHandler.END

    report_id = parse_report_id((query.data or "").strip(), CALLBACK_MOD_REPLY_PREFIX)
    if report_id is None:
        await query.message.reply_text("Не удалось определить заявку.")
        return ConversationHandler.END

    row = get_report(report_id)
    if row is None:
        await query.message.reply_text("Заявка не найдена.")
        return ConversationHandler.END

    context.user_data["mod_reply_report_id"] = report_id
    await query.message.reply_text(
        f"Ответ игроку по заявке #{report_id}:\n"
        "Отправь текст одним сообщением."
    )
    return STATE_MOD_REPLY_TEXT


async def mod_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or msg.text is None or user is None:
        return STATE_MOD_REPLY_TEXT

    if not is_moderator(user.id):
        await msg.reply_text("Нет прав.")
        return ConversationHandler.END

    report_id = int(context.user_data.get("mod_reply_report_id", 0) or 0)
    if report_id <= 0:
        await msg.reply_text("Сессия ответа не найдена.")
        return ConversationHandler.END

    row = get_report(report_id)
    if row is None:
        await msg.reply_text("Заявка не найдена.")
        context.user_data.pop("mod_reply_report_id", None)
        return ConversationHandler.END

    text = msg.text.strip()
    if len(text) < 2:
        await msg.reply_text("Слишком коротко. Напиши подробнее:")
        return STATE_MOD_REPLY_TEXT

    await notify_user(
        context.application,
        int(row["telegram_user_id"]),
        f"Ответ модератора по заявке #{report_id}:\n{text}",
    )
    await msg.reply_text(f"Ответ по заявке #{report_id} отправлен игроку.")
    context.user_data.pop("mod_reply_report_id", None)
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _ = context
    if update.effective_message is not None:
        await update.effective_message.reply_text(
            "Действие отменено.",
            reply_markup=build_main_keyboard(),
        )
    return ConversationHandler.END


async def myreports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return
    await msg.reply_text(user_reports_text(user.id), reply_markup=build_main_keyboard())


async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(RULES_TEXT, reply_markup=build_main_keyboard())


async def support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(SUPPORT_TEXT, reply_markup=build_main_keyboard())


async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return

    if not is_moderator(user.id):
        await msg.reply_text("Нет прав.")
        return

    if not context.args:
        await msg.reply_text("Использование: /close <id>")
        return

    try:
        report_id = int(context.args[0])
    except ValueError:
        await msg.reply_text("ID должен быть числом.")
        return

    ok = close_report(report_id, user.id)
    if not ok:
        await msg.reply_text(f"Заявка #{report_id} не найдена или уже закрыта.")
        return

    row = get_report(report_id)
    if row is not None:
        await notify_user(
            context.application,
            int(row["telegram_user_id"]),
            (
                f"Заявка #{report_id} закрыта модератором. "
                "Если есть новые факты, создай новую заявку."
            ),
        )

    await msg.reply_text(f"Заявка #{report_id} закрыта.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = update
    log.exception("Unhandled exception: %s", context.error)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty. Set it in .env.")

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    init_db()

    app = Application.builder().token(TOKEN).build()

    report_conv = ConversationHandler(
        entry_points=[
            CommandHandler("report", report_start),
            CommandHandler("event", event_start),
            CommandHandler("other", other_start),
            CallbackQueryHandler(
                start_ticket_callback,
                pattern=rf"^{CALLBACK_START_PREFIX}",
            ),
            MessageHandler(filters.Regex(rf"^{MENU_COMPLAINT}$"), menu_report_start),
            MessageHandler(filters.Regex(rf"^{MENU_EVENT}$"), menu_event_start),
            MessageHandler(filters.Regex(rf"^{MENU_OTHER}$"), menu_other_start),
        ],
        states={
            STATE_NICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_nick)],
            STATE_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, report_reason)
            ],
            STATE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_time)],
            STATE_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, report_details)
            ],
            STATE_CONTACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, report_contact)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            MessageHandler(filters.Regex(rf"^{MENU_CANCEL}$"), cancel_cmd),
        ],
        allow_reentry=True,
    )

    add_details_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                user_add_start,
                pattern=rf"^{CALLBACK_USER_ADD_PREFIX}\d+$",
            )
        ],
        states={
            STATE_ADD_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_add_details)
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            MessageHandler(filters.Regex(rf"^{MENU_CANCEL}$"), cancel_cmd),
        ],
        allow_reentry=True,
    )

    mod_reply_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                mod_reply_start,
                pattern=rf"^{CALLBACK_MOD_REPLY_PREFIX}\d+$",
            )
        ],
        states={
            STATE_MOD_REPLY_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, mod_reply_text)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))

    app.add_handler(report_conv)
    app.add_handler(add_details_conv)
    app.add_handler(mod_reply_conv)

    app.add_handler(
        CallbackQueryHandler(
            dashboard_callback,
            pattern=r"^menu_(myreports|rules|support)$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            mod_action_callback,
            pattern=r"^(mod_take:|mod_close:)\d+$",
        )
    )

    app.add_handler(CommandHandler("myreports", myreports_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("support", support_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(MessageHandler(filters.Regex(rf"^{MENU_MY_REPORTS}$"), myreports_cmd))
    app.add_handler(MessageHandler(filters.Regex(rf"^{MENU_RULES}$"), rules_cmd))
    app.add_handler(MessageHandler(filters.Regex(rf"^{MENU_SUPPORT}$"), support_cmd))
    app.add_handler(MessageHandler(filters.Regex(rf"^{MENU_CANCEL}$"), cancel_cmd))

    app.add_error_handler(error_handler)

    if TELEGRAM_MODE == "webhook":
        webhook_base_url = WEBHOOK_BASE_URL or RENDER_EXTERNAL_URL
        if not webhook_base_url:
            raise RuntimeError(
                "WEBHOOK_BASE_URL is empty in webhook mode (or missing RENDER_EXTERNAL_URL)."
            )
        if not WEBHOOK_PATH:
            raise RuntimeError("WEBHOOK_PATH is empty.")
        secret_token = WEBHOOK_SECRET or None
        if secret_token and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret_token):
            log.warning(
                "TELEGRAM_WEBHOOK_SECRET has unallowed characters. "
                "Ignoring secret token for webhook registration."
            )
            secret_token = None

        webhook_url = f"{webhook_base_url}/{WEBHOOK_PATH}"
        log.info(
            "Starting webhook mode on 0.0.0.0:%s, url=%s",
            PORT,
            webhook_url,
        )
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=webhook_url,
            secret_token=secret_token,
            bootstrap_retries=10,
            drop_pending_updates=False,
        )
        return

    log.info("Starting polling mode")
    app.run_polling()


if __name__ == "__main__":
    main()
