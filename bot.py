import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
MOD_CHANNEL_ID = int(os.getenv("MOD_CHANNEL_ID", "0").strip() or "0")
DB_PATH = os.getenv("DB_PATH", "reports.db").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0").strip() or "0")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("complaint-bot")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                discord_user_id INTEGER NOT NULL,
                discord_tag TEXT NOT NULL,
                minecraft_nick TEXT NOT NULL,
                approx_time TEXT NOT NULL,
                reason TEXT NOT NULL,
                details TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                closed_by INTEGER,
                closed_at TEXT
            )
            """
        )
        conn.commit()


def create_report(
    discord_user_id: int,
    discord_tag: str,
    minecraft_nick: str,
    approx_time: str,
    reason: str,
    details: Optional[str],
) -> int:
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO reports (
                created_at,
                discord_user_id,
                discord_tag,
                minecraft_nick,
                approx_time,
                reason,
                details
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                discord_user_id,
                discord_tag,
                minecraft_nick,
                approx_time,
                reason,
                details or "",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def close_report(report_id: int, moderator_id: int) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            """
            UPDATE reports
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
            "SELECT * FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        return row


def get_reports_for_user(discord_user_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reports
            WHERE discord_user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (discord_user_id, limit),
        ).fetchall()
        return list(rows)


def build_report_embed(row: sqlite3.Row) -> discord.Embed:
    color = discord.Color.orange() if row["status"] == "open" else discord.Color.green()
    embed = discord.Embed(
        title=f"Report #{row['id']}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Minecraft nick", value=row["minecraft_nick"], inline=True)
    embed.add_field(name="Approx time", value=row["approx_time"], inline=True)
    embed.add_field(name="Status", value=row["status"], inline=True)
    embed.add_field(name="Reason", value=row["reason"], inline=False)
    details = row["details"] if row["details"] else "-"
    embed.add_field(name="Details", value=details[:1024], inline=False)
    embed.set_footer(
        text=f"Author: {row['discord_tag']} ({row['discord_user_id']})"
    )
    return embed


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


async def send_to_mod_channel(embed: discord.Embed) -> None:
    if MOD_CHANNEL_ID == 0:
        return
    channel = bot.get_channel(MOD_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(MOD_CHANNEL_ID)
        except Exception as exc:
            log.warning("Could not fetch mod channel %s: %s", MOD_CHANNEL_ID, exc)
            return
    try:
        await channel.send(embed=embed)
    except Exception as exc:
        log.warning("Could not send message to mod channel: %s", exc)


@bot.event
async def on_ready() -> None:
    init_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %d guild commands", len(synced))
        else:
            synced = await bot.tree.sync()
            log.info("Synced %d global commands", len(synced))
    except Exception as exc:
        log.warning("Command sync failed: %s", exc)
    log.info("Bot is ready as %s", bot.user)


@bot.tree.command(name="report", description="Send a complaint to moderation")
@app_commands.describe(
    minecraft_nick="Your nickname in Minecraft",
    approx_time="Approximate time (example: 21:30 MSK)",
    reason="Reason for complaint",
    details="Optional details or evidence",
)
async def report_cmd(
    interaction: discord.Interaction,
    minecraft_nick: str,
    approx_time: str,
    reason: str,
    details: Optional[str] = None,
) -> None:
    if len(minecraft_nick) > 32:
        await interaction.response.send_message(
            "Nickname is too long (max 32).",
            ephemeral=True,
        )
        return
    if len(reason) < 4:
        await interaction.response.send_message(
            "Reason is too short.",
            ephemeral=True,
        )
        return

    report_id = create_report(
        discord_user_id=interaction.user.id,
        discord_tag=str(interaction.user),
        minecraft_nick=minecraft_nick.strip(),
        approx_time=approx_time.strip(),
        reason=reason.strip(),
        details=(details or "").strip(),
    )
    row = get_report(report_id)
    if row:
        await send_to_mod_channel(build_report_embed(row))

    await interaction.response.send_message(
        f"Report #{report_id} created. Moderators will review it.",
        ephemeral=True,
    )


@bot.tree.command(name="report_my", description="Show your recent reports")
async def report_my_cmd(interaction: discord.Interaction) -> None:
    rows = get_reports_for_user(interaction.user.id, limit=10)
    if not rows:
        await interaction.response.send_message(
            "You have no reports yet.",
            ephemeral=True,
        )
        return
    lines = []
    for row in rows:
        lines.append(
            f"#{row['id']} | {row['status']} | {row['minecraft_nick']} | {row['reason'][:80]}"
        )
    await interaction.response.send_message(
        "Your reports:\n" + "\n".join(lines),
        ephemeral=True,
    )


@bot.tree.command(name="report_close", description="Close a report (moderators only)")
@app_commands.describe(report_id="Report ID")
async def report_close_cmd(interaction: discord.Interaction, report_id: int) -> None:
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "This command can be used only in a server.",
            ephemeral=True,
        )
        return
    if not member.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "No permission.",
            ephemeral=True,
        )
        return
    ok = close_report(report_id, member.id)
    if not ok:
        await interaction.response.send_message(
            f"Report #{report_id} not found or already closed.",
            ephemeral=True,
        )
        return
    row = get_report(report_id)
    if row:
        embed = build_report_embed(row)
        embed.title = f"Report #{report_id} closed"
        await send_to_mod_channel(embed)
    await interaction.response.send_message(
        f"Report #{report_id} closed.",
        ephemeral=True,
    )


def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is empty. Set it in .env.")
    init_db()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
