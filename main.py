"""
VorstyAFK Discord invite-plan bot for Render Web Services.

Environment variables:
  DISCORD_TOKEN                 Required Discord bot token
  GUILD_ID                      Optional; fast guild-only slash-command sync
  PLAN_ALERT_CHANNEL_ID         Optional channel for plan activation alerts
  EXPIRY_ALERT_CHANNEL_ID       Optional channel for expiration alerts
  UPGRADE_ALERT_CHANNEL_ID      Optional channel for upgrade alerts
  DATABASE_PATH                 Optional SQLite path (default: data/vorstyafk.sqlite3)
  PORT                          Render provides this automatically

Render:
  Build command: pip install -r requirements.txt
  Start command: python main.py

Important:
- Enable Server Members Intent and Message Content Intent in Discord Developer Portal.
- Invite tracking requires the bot to have Manage Server permission.
- SQLite on Render's ephemeral filesystem can reset when the instance is replaced unless
  you attach persistent storage. Use a persistent disk or external database for durable data.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, jsonify

# ----------------------------- Configuration -----------------------------

def _int_env(primary: str, fallback: str = "") -> Optional[int]:
    value = os.getenv(primary, "").strip() or os.getenv(fallback, "").strip()
    return int(value) if value.isdigit() else None


TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
GUILD_ID_RAW = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW.isdigit() else None
PORT = int(os.getenv("PORT", "10000"))

PLAN_ALERT_CHANNEL_ID = _int_env("PLAN_ALERT_CHANNEL_ID", "ACTIVATION_CHANNEL_ID")
EXPIRY_ALERT_CHANNEL_ID = _int_env("EXPIRY_ALERT_CHANNEL_ID", "EXPIRE_CHANNEL_ID")
UPGRADE_ALERT_CHANNEL_ID = _int_env("UPGRADE_ALERT_CHANNEL_ID", "UPGRADE_CHANNEL_ID")

DB_PATH = Path(os.getenv("DATABASE_PATH", "data/vorstyafk.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Invite costs and plan lengths, as requested.
PLANS = {
    "7d": {"label": "7 Days", "invites": 2, "days": 7},
    "14d": {"label": "14 Days", "invites": 4, "days": 14},
    "23d": {"label": "23 Days", "invites": 8, "days": 23},
    "34d": {"label": "34 Days", "invites": 12, "days": 34},
    "60d": {"label": "60 Days", "invites": 16, "days": 60},
    "365d": {"label": "365 Days", "invites": 24, "days": 365},
}
PLAN_CHOICES = [
    app_commands.Choice(name=f'{p["label"]} — {p["invites"]} invites', value=key)
    for key, p in PLANS.items()
]

# ----------------------------- Logging -----------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("VorstyAFK")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def iso_to_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ----------------------------- Database ----------------------------------

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_session():
    conn = db_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_session() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                invites_earned INTEGER NOT NULL DEFAULT 0,
                invites_spent INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS plans (
                user_id INTEGER PRIMARY KEY,
                plan_key TEXT NOT NULL,
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                warned_expiry INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS invite_events (
                guild_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                inviter_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, member_id)
            );
            """
        )


def ensure_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))


def get_user_stats(user_id: int) -> dict:
    with db_session() as conn:
        ensure_user(conn, user_id)
        row = conn.execute(
            "SELECT invites_earned, invites_spent FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        active = conn.execute("SELECT * FROM plans WHERE user_id=?", (user_id,)).fetchone()
    earned = int(row["invites_earned"])
    spent = int(row["invites_spent"])
    plan_data = dict(active) if active else None
    if plan_data and iso_to_dt(plan_data["expires_at"]) <= utcnow():
        plan_data = None
    return {
        "earned": earned,
        "spent": spent,
        "available": max(0, earned - spent),
        "plan": plan_data,
    }


def add_invite_credit(user_id: int, guild_id: int, member_id: int) -> bool:
    """Credit an inviter once per joining member per guild."""
    with db_session() as conn:
        ensure_user(conn, user_id)
        try:
            conn.execute(
                "INSERT INTO invite_events(guild_id, member_id, inviter_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, member_id, user_id, dt_to_iso(utcnow())),
            )
        except sqlite3.IntegrityError:
            return False
        conn.execute(
            "UPDATE users SET invites_earned=invites_earned+1 WHERE user_id=?",
            (user_id,),
        )
        return True


def active_plan_row(user_id: int) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM plans WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    if iso_to_dt(result["expires_at"]) <= utcnow():
        return None
    return result


def create_or_replace_plan(user_id: int, plan_key: str, spend: int) -> tuple[datetime, datetime]:
    now = utcnow()
    expires = now + timedelta(days=PLANS[plan_key]["days"])
    with db_session() as conn:
        ensure_user(conn, user_id)
        row = conn.execute(
            "SELECT invites_earned, invites_spent FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        available = int(row["invites_earned"]) - int(row["invites_spent"])
        if available < spend:
            raise ValueError("NOT_ENOUGH_INVITES")
        conn.execute(
            "UPDATE users SET invites_spent=invites_spent+? WHERE user_id=?",
            (spend, user_id),
        )
        conn.execute(
            """
            INSERT INTO plans(user_id, plan_key, started_at, expires_at, warned_expiry)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                plan_key=excluded.plan_key,
                started_at=excluded.started_at,
                expires_at=excluded.expires_at,
                warned_expiry=0
            """,
            (user_id, plan_key, dt_to_iso(now), dt_to_iso(expires)),
        )
    return now, expires


# ----------------------------- Discord bot -------------------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="$", intents=intents, case_insensitive=True, help_command=None)
invite_cache: dict[int, dict[str, int]] = {}
expiry_task_started = False


async def send_channel_alert(channel_id: Optional[int], message: str) -> None:
    if not channel_id:
        return
    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        await channel.send(message)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.warning("Could not send alert to channel %s: %s", channel_id, exc)


async def send_user_dm(user_id: int, message: str) -> None:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(message)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.info("Could not DM user %s: %s", user_id, exc)


async def refresh_invites(guild: discord.Guild) -> None:
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning(
            "Could not cache invites for guild %s; grant Manage Server permission. %s",
            guild.id,
            exc,
        )


@bot.event
async def on_ready() -> None:
    global expiry_task_started
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")

    for guild in bot.guilds:
        await refresh_invites(guild)

    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            log.info("Synced %d slash commands to guild %s", len(synced), GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Synced %d global slash commands", len(synced))
    except (discord.HTTPException, discord.Forbidden) as exc:
        log.exception("Slash command sync failed: %s", exc)

    if not expiry_task_started:
        expiry_task_started = True
        asyncio.create_task(expiry_monitor())


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    await refresh_invites(guild)


@bot.event
async def on_invite_create(invite: discord.Invite) -> None:
    if invite.guild:
        invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0


@bot.event
async def on_member_join(member: discord.Member) -> None:
    guild = member.guild
    old = invite_cache.get(guild.id, {})
    try:
        fresh_invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Cannot inspect used invites in %s: %s", guild.id, exc)
        return

    new_cache = {invite.code: invite.uses or 0 for invite in fresh_invites}
    used_invite = None
    for invite in fresh_invites:
        if (invite.uses or 0) > old.get(invite.code, 0):
            used_invite = invite
            break
    invite_cache[guild.id] = new_cache

    if used_invite and used_invite.inviter and used_invite.inviter.id != member.id:
        added = add_invite_credit(used_invite.inviter.id, guild.id, member.id)
        if added:
            stats = get_user_stats(used_invite.inviter.id)
            await send_user_dm(
                used_invite.inviter.id,
                f"🎉 Your invite was used in **{guild.name}**!\n"
                f"Available invites: **{stats['available']}**",
            )


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    # Invite credits are lifetime credits and are not removed when a member leaves.
    return


async def expiry_monitor() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = utcnow()
        try:
            with db_session() as conn:
                rows = conn.execute("SELECT * FROM plans").fetchall()
                for row in rows:
                    expiry = iso_to_dt(row["expires_at"])
                    if expiry <= now:
                        user_id = int(row["user_id"])
                        key = row["plan_key"]
                        conn.execute("DELETE FROM plans WHERE user_id=?", (user_id,))
                        message = (
                            f"⏰ <@{user_id}> your **{PLANS.get(key, {}).get('label', key)}** "
                            "plan has expired."
                        )
                        await send_channel_alert(EXPIRY_ALERT_CHANNEL_ID, message)
                        await send_user_dm(user_id, "⏰ Your VorstyAFK plan has expired.")
        except Exception:
            log.exception("Expiry monitor encountered an error")
        await asyncio.sleep(60)


# ----------------------------- Prefix commands ---------------------------

@bot.command(name="i", aliases=["invites", "invite"])
async def prefix_invites(ctx: commands.Context) -> None:
    stats = get_user_stats(ctx.author.id)
    await ctx.reply(
        f"**{ctx.author.display_name}'s invite stats**\n"
        f"• Earned: **{stats['earned']}**\n"
        f"• Spent: **{stats['spent']}**\n"
        f"• Available: **{stats['available']}**",
        mention_author=False,
    )


@bot.command(name="help")
async def prefix_help(ctx: commands.Context) -> None:
    await ctx.reply(
        "**VorstyAFK Commands**\n"
        "• `$i` — invite stats\n"
        "• `$plans` — show available plans\n"
        "• `/i` — invite stats (slash)\n"
        "• `/plans` — show plans (slash)\n"
        "• `/claim plan:<plan>` — claim a plan using invites\n"
        "• `/upgrade plan:<higher plan>` — upgrade your active plan\n"
        "• `/help` — help menu\n\n"
        "Plan claiming is slash-only. Prefix: `$`",
        mention_author=False,
    )


@bot.command(name="plans")
async def prefix_plans(ctx: commands.Context) -> None:
    await ctx.reply(embed=make_plans_embed(), mention_author=False)


def make_plans_embed() -> discord.Embed:
    embed = discord.Embed(
        title="VorstyAFK Plans",
        description="Use your available invite credits to activate one plan.",
        color=discord.Color.blurple(),
    )
    for key, plan in PLANS.items():
        embed.add_field(
            name=plan["label"],
            value=f"Cost: **{plan['invites']} invites**",
            inline=True,
        )
    return embed


# ----------------------------- Slash commands ----------------------------

@bot.tree.command(name="help", description="Show VorstyAFK commands")
async def slash_help(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "**VorstyAFK Commands**\n"
        "• `$i` — invite stats\n"
        "• `$plans` — show available plans\n"
        "• `/i` — invite stats\n"
        "• `/plans` — show plans\n"
        "• `/claim` — claim a plan (slash-only)\n"
        "• `/upgrade` — upgrade your active plan\n"
        "• Prefix: `$`",
        ephemeral=True,
    )


@bot.tree.command(name="i", description="View your invite credits")
async def slash_invites(interaction: discord.Interaction) -> None:
    stats = get_user_stats(interaction.user.id)
    plan = stats["plan"]
    active_line = "None"
    if plan:
        active_line = (
            f"{PLANS.get(plan['plan_key'], {}).get('label', plan['plan_key'])} "
            f"(expires <t:{int(iso_to_dt(plan['expires_at']).timestamp())}:R>)"
        )
    await interaction.response.send_message(
        f"**Your invite stats**\n"
        f"• Earned: **{stats['earned']}**\n"
        f"• Spent: **{stats['spent']}**\n"
        f"• Available: **{stats['available']}**\n"
        f"• Active plan: **{active_line}**",
        ephemeral=True,
    )


@bot.tree.command(name="plans", description="Show all invite plans")
async def slash_plans(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=make_plans_embed(), ephemeral=True)


@bot.tree.command(name="claim", description="Claim a plan using your available invite credits")
@app_commands.describe(plan="Choose the plan to activate")
@app_commands.choices(plan=PLAN_CHOICES)
async def slash_claim(interaction: discord.Interaction, plan: app_commands.Choice[str]) -> None:
    user_id = interaction.user.id
    current = active_plan_row(user_id)
    if current:
        expires = int(iso_to_dt(current["expires_at"]).timestamp())
        await interaction.response.send_message(
            f"You already have an active **{PLANS[current['plan_key']]['label']}** plan "
            f"(expires <t:{expires}:R>). Use `/upgrade` to upgrade it.",
            ephemeral=True,
        )
        return

    plan_key = plan.value
    try:
        _, expires_at = create_or_replace_plan(
            user_id, plan_key, PLANS[plan_key]["invites"]
        )
    except ValueError:
        stats = get_user_stats(user_id)
        await interaction.response.send_message(
            f"Not enough invites. **{PLANS[plan_key]['label']}** costs "
            f"**{PLANS[plan_key]['invites']}**, you have **{stats['available']}** available.",
            ephemeral=True,
        )
        return

    expires_ts = int(expires_at.timestamp())
    await interaction.response.send_message(
        f"✅ Activated **{PLANS[plan_key]['label']}**! "
        f"It expires <t:{expires_ts}:R>.",
        ephemeral=True,
    )
    alert = (
        f"✅ <@{user_id}> activated **{PLANS[plan_key]['label']}** "
        f"for {PLANS[plan_key]['invites']} invites. Expires <t:{expires_ts}:F>."
    )
    await send_channel_alert(PLAN_ALERT_CHANNEL_ID, alert)
    await send_user_dm(
        user_id,
        f"✅ Your **{PLANS[plan_key]['label']}** plan is active until "
        f"<t:{expires_ts}:F>.",
    )


@bot.tree.command(name="upgrade", description="Upgrade your current active plan")
@app_commands.describe(plan="Choose a higher plan")
@app_commands.choices(plan=PLAN_CHOICES)
async def slash_upgrade(interaction: discord.Interaction, plan: app_commands.Choice[str]) -> None:
    user_id = interaction.user.id
    current = active_plan_row(user_id)
    if not current:
        await interaction.response.send_message(
            "You do not have an active plan. Use `/claim` first.",
            ephemeral=True,
        )
        return

    old_key = current["plan_key"]
    new_key = plan.value
    old_cost = PLANS[old_key]["invites"]
    new_cost = PLANS[new_key]["invites"]

    if new_cost <= old_cost:
        await interaction.response.send_message(
            "Choose a higher-cost plan to upgrade. Downgrades are not supported.",
            ephemeral=True,
        )
        return

    stats = get_user_stats(user_id)
    if stats["available"] < 2:
        await interaction.response.send_message(
            f"Upgrade requires at least **2 available invites**. You have "
            f"**{stats['available']}**.",
            ephemeral=True,
        )
        return

    # Charge only the difference between the old and new plan costs.
    additional_cost = new_cost - old_cost
    if stats["available"] < additional_cost:
        await interaction.response.send_message(
            f"Upgrade to **{PLANS[new_key]['label']}** needs **{additional_cost}** "
            f"additional invites (difference between plans); you have "
            f"**{stats['available']}** available.",
            ephemeral=True,
        )
        return

    try:
        _, expires_at = create_or_replace_plan(user_id, new_key, additional_cost)
    except ValueError:
        await interaction.response.send_message(
            "Invite balance changed while processing. Please run `/upgrade` again.",
            ephemeral=True,
        )
        return

    expires_ts = int(expires_at.timestamp())
    await interaction.response.send_message(
        f"⬆️ Upgraded from **{PLANS[old_key]['label']}** to "
        f"**{PLANS[new_key]['label']}**. New expiry: <t:{expires_ts}:F>.",
        ephemeral=True,
    )
    await send_channel_alert(
        UPGRADE_ALERT_CHANNEL_ID,
        f"⬆️ <@{user_id}> upgraded **{PLANS[old_key]['label']}** → "
        f"**{PLANS[new_key]['label']}** (spent {additional_cost} additional invites). "
        f"Expires <t:{expires_ts}:F>.",
    )
    await send_user_dm(
        user_id,
        f"⬆️ Your plan was upgraded to **{PLANS[new_key]['label']}**. "
        f"It expires <t:{expires_ts}:F>.",
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(f"Please wait {error.retry_after:.1f}s before trying again.", mention_author=False)
        return
    log.error("Prefix command error: %s", error, exc_info=(type(error), error, error.__traceback__))
    try:
        await ctx.reply("Something went wrong while running that command. Check the bot logs.", mention_author=False)
    except discord.HTTPException:
        pass


# ----------------------------- Flask health endpoint ---------------------

web_app = Flask("vorstyafk")


@web_app.get("/")
def health():
    return jsonify(
        service="VorstyAFK",
        status="online" if bot.is_ready() else "starting",
        bot_ready=bot.is_ready(),
    ), 200


@web_app.get("/health")
def health_check():
    return jsonify(ok=True, bot_ready=bot.is_ready()), 200


def run_web_server() -> None:
    web_app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)


def start_web_server() -> None:
    Thread(target=run_web_server, name="render-health-server", daemon=True).start()


# ----------------------------- Entrypoint --------------------------------

if __name__ == "__main__":
    init_db()
    start_web_server()

    if not TOKEN:
        raise RuntimeError(
            "Missing Discord token. Set DISCORD_TOKEN (recommended) or BOT_TOKEN in Render Environment."
        )

    bot.run(TOKEN, log_handler=None)
