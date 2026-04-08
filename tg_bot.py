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


STATE_NICK, STATE_TIME, STATE_REASON, STATE_DETAILS, STATE_CONTACT = range(5)

TICKET_TYPE_COMPLAINT = "complaint"
TICKET_TYPE_EVENT = "event"
TICKET_TYPE_OTHER = "other"
CALLBACK_START_PREFIX = "ticket_start:"

TICKET_TYPE_META = {
    TICKET_TYPE_COMPLAINT: {
        "label": "Жалоба",
        "reason_prompt": "Шаг 3/5. Причина жалобы (кратко):",
    },
    TICKET_TYPE_EVENT: {
        "label": "Участие в ивенте",
        "reason_prompt": "Шаг 3/5. В какой ивент хочешь участвовать:",
    },
    TICKET_TYPE_OTHER: {
        "label": "Другое обращение",
        "reason_prompt": "Шаг 3/5. Тема обращения:",
    },
}

MENU_COMPLAINT = "Жалоба"
MENU_EVENT = "Ивент"
MENU_OTHER = "Другое"
MENU_MY_REPORTS = "Мои обращения"
MENU_CANCEL = "Отмена"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    ticket_type = normalize_ticket_type(ticket_type)
    return TICKET_TYPE_META[ticket_type]["label"]


def ticket_reason_prompt(ticket_type: str) -> str:
    ticket_type = normalize_ticket_type(ticket_type)
    return TICKET_TYPE_META[ticket_type]["reason_prompt"]


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
                approx_time TEXT NOT NULL,
                reason TEXT NOT NULL,
                details TEXT,
                contact TEXT NOT NULL DEFAULT '',
                ticket_type TEXT NOT NULL DEFAULT 'complaint',
                status TEXT NOT NULL DEFAULT 'open',
                closed_by INTEGER,
                closed_at TEXT
            )
            """
        )
        ensure_column(
            conn,
            "tg_reports",
            "contact",
            "TEXT NOT NULL DEFAULT ''",
        )
        ensure_column(
            conn,
            "tg_reports",
            "ticket_type",
            "TEXT NOT NULL DEFAULT 'complaint'",
        )
        conn.commit()


def create_report(
    telegram_user_id: int,
    telegram_username: str,
    telegram_name: str,
    ticket_type: str,
    minecraft_nick: str,
    approx_time: str,
    reason: str,
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
                approx_time,
                reason,
                details,
                contact
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                telegram_user_id,
                telegram_username,
                telegram_name,
                normalize_ticket_type(ticket_type),
                minecraft_nick,
                approx_time,
                reason,
                details or "",
                contact or "",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def close_report(report_id: int, moderator_id: int) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            """
            UPDATE tg_reports
            SET status = 'closed',
                closed_by = ?,
                closed_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (moderator_id, now_iso(), report_id),
        )
        conn.commit()
        return cur.rowcount > 0


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


def format_report_for_mod(row: sqlite3.Row) -> str:
    ticket_type = normalize_ticket_type((row["ticket_type"] or "").strip())
    lines = [
        f"{SERVER_NAME} | Обращение #{row['id']}",
        f"Тип: {ticket_label(ticket_type)}",
        f"Статус: {row['status']}",
        f"Ник Minecraft: {row['minecraft_nick']}",
        f"Примерное время: {row['approx_time']}",
        f"Причина/тема: {row['reason']}",
        f"Детали: {row['details'] or '-'}",
        f"Контакт: {row['contact'] or '-'}",
        (
            f"Автор: {row['telegram_name']} "
            f"(@{row['telegram_username'] or 'no_username'}, id={row['telegram_user_id']})"
        ),
    ]
    return "\n".join(lines)


async def send_to_mod_chat(application: Application, row: sqlite3.Row) -> None:
    if MOD_CHAT_ID == 0:
        return
    try:
        await application.bot.send_message(
            chat_id=MOD_CHAT_ID,
            text=format_report_for_mod(row),
        )
    except Exception as exc:
        log.warning("Could not send report to moderation chat: %s", exc)


def is_moderator(user_id: int) -> bool:
    return user_id in MOD_USER_IDS


def build_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_COMPLAINT, MENU_EVENT, MENU_OTHER],
            [MENU_MY_REPORTS, MENU_CANCEL],
            ["/help"],
        ],
        resize_keyboard=True,
    )


def build_start_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    MENU_COMPLAINT,
                    callback_data=f"{CALLBACK_START_PREFIX}{TICKET_TYPE_COMPLAINT}",
                ),
                InlineKeyboardButton(
                    MENU_EVENT,
                    callback_data=f"{CALLBACK_START_PREFIX}{TICKET_TYPE_EVENT}",
                ),
            ],
            [
                InlineKeyboardButton(
                    MENU_OTHER,
                    callback_data=f"{CALLBACK_START_PREFIX}{TICKET_TYPE_OTHER}",
                )
            ],
        ]
    )


def is_skip_value(value: str) -> bool:
    return value.strip().lower() in {"-", "none", "no", "нет", "пропустить"}


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        f"{SERVER_NAME} | Telegram Support Bot\n\n"
        "Нажми кнопку ниже и заполни обращение.\n\n"
        "Доступные команды:\n"
        "/report - подать жалобу\n"
        "/event - заявка на участие в ивенте\n"
        "/other - другое обращение\n"
        "/myreports - мои последние обращения\n"
        "/close <id> - закрыть обращение (модераторы)\n"
        "/cancel - отменить заполнение",
        reply_markup=build_start_inline_keyboard(),
    )
    await update.effective_message.reply_text(
        "Быстрое меню:",
        reply_markup=build_main_keyboard(),
    )


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
    if update.effective_message is None or update.effective_message.text is None:
        return STATE_NICK
    nick = update.effective_message.text.strip()
    if not nick:
        await update.effective_message.reply_text("Ник не может быть пустым. Попробуй еще:")
        return STATE_NICK
    if len(nick) > 32:
        await update.effective_message.reply_text("Ник слишком длинный (макс. 32). Попробуй еще:")
        return STATE_NICK
    context.user_data["minecraft_nick"] = nick
    await update.effective_message.reply_text(
        "Шаг 2/5. Укажи примерное время (например: 21:30 МСК):"
    )
    return STATE_TIME


async def report_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None or update.effective_message.text is None:
        return STATE_TIME
    approx_time = update.effective_message.text.strip()
    if not approx_time:
        await update.effective_message.reply_text("Время не может быть пустым. Попробуй еще:")
        return STATE_TIME
    context.user_data["approx_time"] = approx_time
    ticket_type = normalize_ticket_type(
        context.user_data.get("ticket_type", TICKET_TYPE_COMPLAINT).strip()
    )
    await update.effective_message.reply_text(ticket_reason_prompt(ticket_type))
    return STATE_REASON


async def report_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None or update.effective_message.text is None:
        return STATE_REASON
    reason = update.effective_message.text.strip()
    if len(reason) < 3:
        await update.effective_message.reply_text(
            "Слишком коротко. Напиши хотя бы 3 символа:"
        )
        return STATE_REASON
    context.user_data["reason"] = reason
    await update.effective_message.reply_text(
        "Шаг 4/5. Детали / доказательства (или '-' чтобы пропустить):"
    )
    return STATE_DETAILS


async def report_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None or update.effective_message.text is None:
        return STATE_DETAILS
    details = update.effective_message.text.strip()
    if is_skip_value(details):
        details = ""
    context.user_data["details"] = details
    await update.effective_message.reply_text(
        "Шаг 5/5. Контакт для ответа (Discord/TG) или '-' чтобы пропустить:"
    )
    return STATE_CONTACT


async def report_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return STATE_CONTACT
    user = update.effective_user
    if user is None:
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
        approx_time=context.user_data.get("approx_time", "").strip(),
        reason=context.user_data.get("reason", "").strip(),
        details=context.user_data.get("details", "").strip(),
        contact=contact,
    )

    row = get_report(report_id)
    if row is not None:
        await send_to_mod_chat(context.application, row)

    await msg.reply_text(
        f"Готово. Обращение #{report_id} отправлено модерации {SERVER_NAME}.",
        reply_markup=build_main_keyboard(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _ = context
    if update.effective_message is not None:
        await update.effective_message.reply_text(
            "Заполнение отменено.",
            reply_markup=build_main_keyboard(),
        )
    return ConversationHandler.END


async def menu_myreports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await myreports_cmd(update, context)


async def menu_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cancel_cmd(update, context)


async def myreports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return
    rows = get_reports_for_user(user.id, limit=10)
    if not rows:
        await msg.reply_text("Пока нет обращений.", reply_markup=build_main_keyboard())
        return

    lines = []
    for row in rows:
        reason = row["reason"] or ""
        ticket_type = normalize_ticket_type((row["ticket_type"] or "").strip())
        lines.append(
            f"#{row['id']} | {ticket_label(ticket_type)} | {row['status']} | {reason[:60]}"
        )
    await msg.reply_text(
        "Твои обращения:\n" + "\n".join(lines),
        reply_markup=build_main_keyboard(),
    )


async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return

    if not is_moderator(user.id):
        await msg.reply_text("Нет прав на эту команду.")
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
        await msg.reply_text(f"Обращение #{report_id} не найдено или уже закрыто.")
        return

    row = get_report(report_id)
    if row is not None:
        await send_to_mod_chat(context.application, row)

    await msg.reply_text(f"Обращение #{report_id} закрыто.")


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
            STATE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_time)],
            STATE_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, report_reason)
            ],
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

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(report_conv)
    app.add_handler(CommandHandler("myreports", myreports_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(
        MessageHandler(filters.Regex(rf"^{MENU_MY_REPORTS}$"), menu_myreports_cmd)
    )
    app.add_handler(MessageHandler(filters.Regex(rf"^{MENU_CANCEL}$"), menu_cancel_cmd))
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
        url_path = WEBHOOK_PATH
        webhook_url = f"{webhook_base_url}/{url_path}"
        log.info(
            "Starting webhook mode on 0.0.0.0:%s, url=%s",
            PORT,
            webhook_url,
        )
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=url_path,
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
