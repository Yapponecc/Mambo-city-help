# Mambo City Support Bot (Discord + Telegram)

Telegram bot with button menu for `Mambo City`:
- complaint (`/report`)
- event application (`/event`)
- other request (`/other`)
- my requests (`/myreports`)
- close request (`/close <id>`, moderators only)
- dashboard buttons: rules + support
- user can add extra details to an open report
- moderator panel buttons in mod chat: take / close / reply

Report form flow:
1. Minecraft nick
2. Reason
3. Incident time
4. Details/evidence
5. Contact

## Quick Start (local)

1. Copy `.env.example` to `.env`
2. Fill:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_MOD_CHAT_ID`
   - `TELEGRAM_MOD_USER_IDS`
   - `SERVER_NAME=Mambo City` (optional)
3. Run:

```bash
cd complaint-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python tg_bot.py
```

## Free deploy: GitHub + Render (Web Service)

This project is configured for Render via [render.yaml](./render.yaml).

### Render env vars

Required:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_MOD_CHAT_ID`
- `TELEGRAM_MOD_USER_IDS`
- `TELEGRAM_WEBHOOK_SECRET` (random long string)

Optional:
- `SERVER_NAME` (default: `Mambo City`)
- `DB_PATH` (default: `reports.db`)
- `WEBHOOK_BASE_URL` (if not set, bot tries `RENDER_EXTERNAL_URL`)
- `WEBHOOK_PATH` (default: `telegram/webhook`)
- `RULES_TEXT`
- `SUPPORT_TEXT`

Auto-set by `render.yaml`:
- `TELEGRAM_MODE=webhook`

### Important note about free storage

`reports.db` is SQLite on local disk. On free cloud instances, local disk is usually ephemeral.
If you need durable history, migrate to external DB (Postgres/MySQL).
Also, free web services can spin down on idle and wake on next request.

## Discord bot (optional)

Run `bot.py` for Discord commands:
- `/report`
- `/report_my`
- `/report_close`
