"""VorstyAFK invite/plan bot for a Render Web Service.

Required Render environment:
  DISCORD_TOKEN = Discord bot token
  GUILD_ID = your one allowed Discord server ID (currently defaults to the ID in prior logs)
Optional:
  DATABASE_PATH, PLAN_ALERT_CHANNEL_ID, EXPIRY_ALERT_CHANNEL_ID,
  UPGRADE_ALERT_CHANNEL_ID, ABUSE_LOG_CHANNEL_ID, PORT

Enable Server Members Intent + Message Content Intent in the Discord Developer Portal.
The bot needs Manage Server permission to inspect invite use. Use persistent storage for SQLite.
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

# ---------------- Configuration ----------------
def env_int(name: str, fallback: Optional[int] = None) -> Optional[int]:
    value = os.getenv(name, "").strip()
    if value.isdigit():
        return int(value)
    return fallback

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
# This fallback is the only guild ID visible in the supplied Render logs.
GUILD_ID = env_int("GUILD_ID", 1559826100153090080)
if not GUILD_ID:
    raise RuntimeError("Set GUILD_ID in Render Environment to your server ID.")
GUILD_OBJECT = discord.Object(id=GUILD_ID)
PORT = env_int("PORT", 10000) or 10000
PLAN_ALERT_CHANNEL_ID = env_int("PLAN_ALERT_CHANNEL_ID", env_int("ACTIVATION_CHANNEL_ID"))
EXPIRY_ALERT_CHANNEL_ID = env_int("EXPIRY_ALERT_CHANNEL_ID", env_int("EXPIRE_CHANNEL_ID"))
UPGRADE_ALERT_CHANNEL_ID = env_int("UPGRADE_ALERT_CHANNEL_ID", env_int("UPGRADE_CHANNEL_ID"))
ABUSE_LOG_CHANNEL_ID = env_int("ABUSE_LOG_CHANNEL_ID")
DB_PATH = Path(os.getenv("DATABASE_PATH", "data/vorstyafk.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
MIN_ACCOUNT_AGE_DAYS = 7

PLANS = {
    "7d": {"label": "7 Days", "invites": 2, "days": 7},
    "14d": {"label": "14 Days", "invites": 4, "days": 14},
    "23d": {"label": "23 Days", "invites": 8, "days": 23},
    "34d": {"label": "34 Days", "invites": 12, "days": 34},
    "60d": {"label": "60 Days", "invites": 16, "days": 60},
    "365d": {"label": "365 Days", "invites": 24, "days": 365},
}
PLAN_CHOICES = [app_commands.Choice(name=f'{p["label"]} — {p["invites"]} invites', value=k) for k, p in PLANS.items()]
STATUS_CHOICES = [
    app_commands.Choice(name="Online", value="online"),
    app_commands.Choice(name="Offline", value="offline"),
    app_commands.Choice(name="Reconnect", value="reconnect"),
    app_commands.Choice(name="Expired", value="expired"),
]

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("VorstyAFK")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def discord_ts(value: Optional[datetime], style: str = "R") -> str:
    return f"<t:{int(value.timestamp())}:{style}>" if value else "Not set"


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    pieces = []
    if days:
        pieces.append(f"{days}d")
    if hours or days:
        pieces.append(f"{hours}h")
    pieces.append(f"{minutes}m")
    return " ".join(pieces)

# ---------------- Database ----------------
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
        conn.executescript("""
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
            status TEXT NOT NULL DEFAULT 'offline',
            status_since TEXT,
            warned_expiry INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS invite_events (
            guild_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            inviter_id INTEGER,
            invite_code TEXT,
            created_at TEXT NOT NULL,
            account_created_at TEXT NOT NULL,
            account_age_days REAL NOT NULL,
            qualified INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(guild_id, member_id)
        );
        """)
        # Safe, additive migrations for databases created by older versions.
        plan_cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
        if "status" not in plan_cols:
            conn.execute("ALTER TABLE plans ADD COLUMN status TEXT NOT NULL DEFAULT 'offline'")
        if "status_since" not in plan_cols:
            conn.execute("ALTER TABLE plans ADD COLUMN status_since TEXT")
        if "warned_expiry" not in plan_cols:
            conn.execute("ALTER TABLE plans ADD COLUMN warned_expiry INTEGER NOT NULL DEFAULT 0")
        event_cols = {r[1] for r in conn.execute("PRAGMA table_info(invite_events)").fetchall()}
        additions = {
            "invite_code": "TEXT",
            "account_created_at": "TEXT NOT NULL DEFAULT ''",
            "account_age_days": "REAL NOT NULL DEFAULT 0",
            "qualified": "INTEGER NOT NULL DEFAULT 0",
            "reason": "TEXT NOT NULL DEFAULT ''",
        }
        for col, declaration in additions.items():
            if col not in event_cols:
                conn.execute(f"ALTER TABLE invite_events ADD COLUMN {col} {declaration}")


def ensure_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))


def get_stats(user_id: int) -> dict:
    with db_session() as conn:
        ensure_user(conn, user_id)
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        plan = conn.execute("SELECT * FROM plans WHERE user_id=?", (user_id,)).fetchone()
        qualified = conn.execute("SELECT COUNT(*) n FROM invite_events WHERE inviter_id=? AND qualified=1", (user_id,)).fetchone()["n"]
        flagged = conn.execute("SELECT COUNT(*) n FROM invite_events WHERE inviter_id=? AND qualified=0", (user_id,)).fetchone()["n"]
    plan_dict = dict(plan) if plan else None
    if plan_dict and from_iso(plan_dict["expires_at"]) and from_iso(plan_dict["expires_at"]) <= now_utc():
        plan_dict["status"] = "expired"
    earned, spent = int(user["invites_earned"]), int(user["invites_spent"])
    return {"earned": earned, "spent": spent, "available": max(0, earned-spent), "qualified": int(qualified), "flagged": int(flagged), "plan": plan_dict}


def active_plan(user_id: int) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM plans WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    expiry = from_iso(result["expires_at"])
    if not expiry or expiry <= now_utc():
        return None
    return result


def credit_invite(inviter_id: int, guild_id: int, member: discord.Member, invite_code: Optional[str], qualified: bool, reason: str) -> bool:
    created = member.created_at.astimezone(timezone.utc)
    age_days = (now_utc() - created).total_seconds() / 86400
    with db_session() as conn:
        ensure_user(conn, inviter_id)
        try:
            conn.execute("""INSERT INTO invite_events
                (guild_id, member_id, inviter_id, invite_code, created_at, account_created_at, account_age_days, qualified, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, member.id, inviter_id, invite_code, to_iso(now_utc()), to_iso(created), age_days, 1 if qualified else 0, reason))
        except sqlite3.IntegrityError:
            return False
        if qualified:
            conn.execute("UPDATE users SET invites_earned=invites_earned+1 WHERE user_id=?", (inviter_id,))
    return True


def change_balance(user_id: int, amount: int, mode: str = "add") -> int:
    with db_session() as conn:
        ensure_user(conn, user_id)
        row = conn.execute("SELECT invites_earned, invites_spent FROM users WHERE user_id=?", (user_id,)).fetchone()
        earned, spent = int(row["invites_earned"]), int(row["invites_spent"])
        if mode == "add":
            earned += amount
        elif mode == "set":
            earned = max(spent, amount)
        elif mode == "remove":
            earned = max(spent, earned - amount)
        conn.execute("UPDATE users SET invites_earned=? WHERE user_id=?", (earned, user_id))
        return max(0, earned-spent)


def activate_plan(user_id: int, plan_key: str, charge: int = 0, admin_grant: bool = False) -> datetime:
    now = now_utc()
    expiry = now + timedelta(days=PLANS[plan_key]["days"])
    with db_session() as conn:
        ensure_user(conn, user_id)
        row = conn.execute("SELECT invites_earned, invites_spent FROM users WHERE user_id=?", (user_id,)).fetchone()
        available = int(row["invites_earned"]) - int(row["invites_spent"])
        if not admin_grant and available < charge:
            raise ValueError("NOT_ENOUGH_INVITES")
        if charge:
            conn.execute("UPDATE users SET invites_spent=invites_spent+? WHERE user_id=?", (charge, user_id))
        conn.execute("""INSERT INTO plans(user_id, plan_key, started_at, expires_at, status, status_since, warned_expiry)
            VALUES (?, ?, ?, ?, 'offline', ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET plan_key=excluded.plan_key, started_at=excluded.started_at,
            expires_at=excluded.expires_at, status='offline', status_since=excluded.status_since, warned_expiry=0""",
            (user_id, plan_key, to_iso(now), to_iso(expiry), to_iso(now)))
    return expiry

# ---------------- Discord bot ----------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="$", intents=intents, case_insensitive=True, help_command=None)
invite_cache: dict[int, dict[str, int]] = {}
expiry_task_started = False


def in_allowed_guild(guild: Optional[discord.Guild]) -> bool:
    return bool(guild and guild.id == GUILD_ID)


async def send_channel(channel_id: Optional[int], message: str) -> None:
    if not channel_id:
        return
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.send(message)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.warning("Could not send to channel %s: %s", channel_id, exc)


async def send_dm(user_id: int, message: str) -> None:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(message)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.info("Could not DM user %s (DMs may be closed): %s", user_id, exc)


async def refresh_invites(guild: discord.Guild) -> None:
    if guild.id != GUILD_ID:
        return
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {i.code: i.uses or 0 for i in invites}
        log.info("Cached %d invites for guild %s", len(invites), guild.id)
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Invite tracking cannot read server invites. Give the bot Manage Server permission. %s", exc)


@bot.event
async def on_ready() -> None:
    global expiry_task_started
    log.info("Logged in as %s (%s); allowed guild=%s", bot.user, bot.user.id if bot.user else "unknown", GUILD_ID)
    for guild in list(bot.guilds):
        if guild.id != GUILD_ID:
            log.warning("Leaving unauthorized server %s", guild.id)
            try:
                await guild.leave()
            except discord.HTTPException:
                log.exception("Could not leave unauthorized guild %s", guild.id)
        else:
            await refresh_invites(guild)
    try:
        # Remove legacy global slash commands, then register only guild-scoped commands.
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        synced = await bot.tree.sync(guild=GUILD_OBJECT)
        log.info("Synced %d server-only slash commands to guild %s", len(synced), GUILD_ID)
    except (discord.HTTPException, discord.Forbidden) as exc:
        log.exception("Slash command sync failed: %s", exc)
    if not expiry_task_started:
        expiry_task_started = True
        asyncio.create_task(expiry_monitor())


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    if guild.id != GUILD_ID:
        log.warning("Leaving unauthorized server %s", guild.id)
        await guild.leave()
    else:
        await refresh_invites(guild)


@bot.event
async def on_invite_create(invite: discord.Invite) -> None:
    if invite.guild and invite.guild.id == GUILD_ID:
        invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if member.guild.id != GUILD_ID or member.bot:
        return
    if member.guild.id not in invite_cache:
        await refresh_invites(member.guild)
        if member.guild.id not in invite_cache:
            log.warning("Skipping invite credit for member %s because invite cache is unavailable", member.id)
            return
    previous = invite_cache.get(member.guild.id, {})
    try:
        fresh = await member.guild.invites()
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Cannot inspect invite use; Manage Server permission required: %s", exc)
        return
    current = {i.code: i.uses or 0 for i in fresh}
    used = next((i for i in fresh if (i.uses or 0) > previous.get(i.code, 0)), None)
    invite_cache[member.guild.id] = current
    if not used or not used.inviter or used.inviter.id == member.id:
        log.info("Could not reliably attribute join %s to an invite", member.id)
        return
    age_days = (now_utc() - member.created_at.astimezone(timezone.utc)).total_seconds() / 86400
    qualified = age_days >= MIN_ACCOUNT_AGE_DAYS
    reason = "qualified" if qualified else f"account younger than {MIN_ACCOUNT_AGE_DAYS} days"
    added = credit_invite(used.inviter.id, member.guild.id, member, used.code, qualified, reason)
    if not added:
        return
    if qualified:
        stats = get_stats(used.inviter.id)
        await send_dm(used.inviter.id, f"🎉 Invite credited in **{member.guild.name}**.\nNew member account age: **{age_days:.1f} days**.\nAvailable invites: **{stats['available']}**")
    else:
        msg = f"⚠️ Possible invite abuse: {member.mention} account is **{age_days:.1f} days** old. Invite credit was **not** given to {used.inviter.mention}."
        await send_channel(ABUSE_LOG_CHANNEL_ID, msg)
        await send_dm(used.inviter.id, f"⚠️ Your invite was not credited: the joining account is only **{age_days:.1f} days** old. Accounts must be at least {MIN_ACCOUNT_AGE_DAYS} days old.")


@bot.check
async def prefix_server_only(ctx: commands.Context) -> bool:
    return bool(ctx.guild and ctx.guild.id == GUILD_ID)


async def expiry_monitor() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = now_utc()
        try:
            with db_session() as conn:
                rows = conn.execute("SELECT * FROM plans").fetchall()
                for row in rows:
                    expiry = from_iso(row["expires_at"])
                    if not expiry:
                        continue
                    user_id, key = int(row["user_id"]), row["plan_key"]
                    if expiry > now and expiry <= now + timedelta(hours=24) and not int(row["warned_expiry"]):
                        conn.execute("UPDATE plans SET warned_expiry=1 WHERE user_id=?", (user_id,))
                        await send_dm(user_id, f"⏳ **VorstyAFK expiry reminder**\nYour **{PLANS.get(key, {}).get('label', key)}** plan expires {discord_ts(expiry, 'F')} (in less than 24 hours).\nCurrent status: **{row['status'].title()}**. Please renew/claim after it expires.")
                        await send_channel(EXPIRY_ALERT_CHANNEL_ID, f"⏳ <@{user_id}> plan expires within 24 hours: {discord_ts(expiry, 'F')}.")
                    elif expiry <= now and row["status"] != "expired":
                        conn.execute("UPDATE plans SET status='expired', status_since=?, warned_expiry=1 WHERE user_id=?", (to_iso(now), user_id))
                        await send_dm(user_id, f"⏰ Your **{PLANS.get(key, {}).get('label', key)}** VorstyAFK plan has expired.")
                        await send_channel(EXPIRY_ALERT_CHANNEL_ID, f"⏰ <@{user_id}> **{PLANS.get(key, {}).get('label', key)}** plan has expired.")
        except Exception:
            log.exception("Expiry monitor encountered an error")
        await asyncio.sleep(60)

# ---------------- Embeds and prefix commands ----------------
def status_text(plan: Optional[dict]) -> str:
    if not plan:
        return "No plan"
    expiry = from_iso(plan.get("expires_at"))
    if not expiry or expiry <= now_utc():
        return "Expired"
    return str(plan.get("status", "offline")).title()


def plan_uptime(plan: Optional[dict]) -> str:
    if not plan or status_text(plan) != "Online":
        return "—"
    since = from_iso(plan.get("status_since"))
    return fmt_duration(int((now_utc() - since).total_seconds())) if since else "—"


def make_plans_embed(user_id: Optional[int] = None) -> discord.Embed:
    embed = discord.Embed(title="VorstyAFK Plans", description="Plans use qualified invite credits. Accounts younger than 7 days do not earn invite credit.", color=discord.Color.blurple())
    for key, plan in PLANS.items():
        embed.add_field(name=plan["label"], value=f"Cost: **{plan['invites']} invites**\nDuration: **{plan['days']} days**", inline=True)
    if user_id is not None:
        stats = get_stats(user_id)
        p = stats["plan"]
        if p:
            expiry = from_iso(p["expires_at"])
            embed.add_field(name="Your current plan", value=(f"Plan: **{PLANS.get(p['plan_key'], {}).get('label', p['plan_key'])}**\n"
                f"Status: **{status_text(p)}**\nUptime: **{plan_uptime(p)}**\nExpires: {discord_ts(expiry, 'F')} ({discord_ts(expiry, 'R')})"), inline=False)
        else:
            embed.add_field(name="Your current plan", value="No active plan. Use `/claim` when you have enough invites.", inline=False)
        embed.add_field(name="Your invite balance", value=f"Available: **{stats['available']}** · Earned: **{stats['earned']}** · Spent: **{stats['spent']}**", inline=False)
    embed.set_footer(text="Plan status is set by server admins; the bot does not guess that a service is online.")
    return embed


@bot.command(name="i", aliases=["invites", "invite", "checkinvite"])
async def prefix_invites(ctx: commands.Context) -> None:
    stats = get_stats(ctx.author.id)
    created = ctx.author.created_at.astimezone(timezone.utc)
    age = now_utc() - created
    embed = discord.Embed(title=f"{ctx.author.display_name} · Invite Check", color=discord.Color.blurple())
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.add_field(name="Invite balance", value=f"Available: **{stats['available']}**\nEarned: **{stats['earned']}**\nSpent: **{stats['spent']}**", inline=True)
    embed.add_field(name="Invite quality", value=f"Qualified joins: **{stats['qualified']}**\nFlagged joins: **{stats['flagged']}**", inline=True)
    embed.add_field(name="Your Discord account", value=f"Created: {discord_ts(created, 'F')}\nAccount age: **{age.days} days**", inline=False)
    p = stats["plan"]
    if p:
        expiry = from_iso(p["expires_at"])
        embed.add_field(name="Plan", value=f"**{PLANS.get(p['plan_key'], {}).get('label', p['plan_key'])}** · **{status_text(p)}**\nUptime: **{plan_uptime(p)}**\nExpires: {discord_ts(expiry, 'R')}", inline=False)
    else:
        embed.add_field(name="Plan", value="No plan active", inline=False)
    embed.set_footer(text="New accounts under 7 days are flagged and do not earn invite credits. This is an age filter, not proof an account is an alt.")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="help")
async def prefix_help(ctx: commands.Context) -> None:
    await ctx.reply("**VorstyAFK**\n`$i` — invite balance and account-age check\n`$plans` — available plans and your plan status\n`$help` — this menu", mention_author=False)


@bot.command(name="plans")
async def prefix_plans(ctx: commands.Context) -> None:
    await ctx.reply(embed=make_plans_embed(ctx.author.id), mention_author=False)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.reply("This bot only works in its configured server.", mention_author=False)
        return
    log.error("Prefix command error: %s", error, exc_info=(type(error), error, error.__traceback__))
    try:
        await ctx.reply("Command failed. Please try again; the error has been logged.", mention_author=False)
    except discord.HTTPException:
        pass

# ---------------- Guild-only slash commands ----------------
@bot.tree.command(name="help", description="Show available VorstyAFK commands", guild=GUILD_OBJECT)
async def slash_help(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("**VorstyAFK**\n`/plans` — view plans, status, uptime and expiry\n`/claim` — activate a plan using invites\n`/upgrade` — upgrade an active plan\n`/admin` — admin-only member controls", ephemeral=True)


@bot.tree.command(name="plans", description="View plans and your current plan status", guild=GUILD_OBJECT)
async def slash_plans(interaction: discord.Interaction) -> None:
    if not in_allowed_guild(interaction.guild):
        await interaction.response.send_message("This bot is only available in its configured server.", ephemeral=True)
        return
    await interaction.response.send_message(embed=make_plans_embed(interaction.user.id), ephemeral=True)


@bot.tree.command(name="claim", description="Claim a plan using qualified invite credits", guild=GUILD_OBJECT)
@app_commands.describe(plan="Plan you want to activate")
@app_commands.choices(plan=PLAN_CHOICES)
async def slash_claim(interaction: discord.Interaction, plan: app_commands.Choice[str]) -> None:
    if not in_allowed_guild(interaction.guild):
        await interaction.response.send_message("This bot is only available in its configured server.", ephemeral=True)
        return
    uid, key = interaction.user.id, plan.value
    if active_plan(uid):
        await interaction.response.send_message("You already have an active plan. Use `/upgrade` or wait for expiry.", ephemeral=True)
        return
    try:
        expiry = activate_plan(uid, key, PLANS[key]["invites"])
    except ValueError:
        stats = get_stats(uid)
        await interaction.response.send_message(f"Not enough invites. **{PLANS[key]['label']}** costs {PLANS[key]['invites']}; you have **{stats['available']}** available.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ **{PLANS[key]['label']}** activated. Expires {discord_ts(expiry, 'F')}.", ephemeral=True)
    await send_channel(PLAN_ALERT_CHANNEL_ID, f"✅ {interaction.user.mention} activated **{PLANS[key]['label']}**. Expires {discord_ts(expiry, 'F')}.")
    await send_dm(uid, f"✅ Your **{PLANS[key]['label']}** plan is active until {discord_ts(expiry, 'F')}. Status starts as **Offline** until an admin changes it.")


@bot.tree.command(name="upgrade", description="Upgrade your current plan", guild=GUILD_OBJECT)
@app_commands.describe(plan="Choose a higher plan")
@app_commands.choices(plan=PLAN_CHOICES)
async def slash_upgrade(interaction: discord.Interaction, plan: app_commands.Choice[str]) -> None:
    if not in_allowed_guild(interaction.guild):
        await interaction.response.send_message("This bot is only available in its configured server.", ephemeral=True)
        return
    uid, new_key = interaction.user.id, plan.value
    current = active_plan(uid)
    if not current:
        await interaction.response.send_message("You do not have an active plan. Use `/claim` first.", ephemeral=True)
        return
    old_key = current["plan_key"]
    difference = PLANS[new_key]["invites"] - PLANS[old_key]["invites"]
    if difference <= 0:
        await interaction.response.send_message("Choose a higher-cost plan. Downgrades are not supported.", ephemeral=True)
        return
    stats = get_stats(uid)
    if stats["available"] < max(2, difference):
        await interaction.response.send_message(f"Upgrade needs **{difference}** additional invites (minimum 2). Available: **{stats['available']}**.", ephemeral=True)
        return
    try:
        expiry = activate_plan(uid, new_key, difference)
    except ValueError:
        await interaction.response.send_message("Invite balance changed. Please run `/upgrade` again.", ephemeral=True)
        return
    await interaction.response.send_message(f"⬆️ Upgraded to **{PLANS[new_key]['label']}**. Status reset to Offline until an admin sets it. Expires {discord_ts(expiry, 'F')}.", ephemeral=True)
    await send_channel(UPGRADE_ALERT_CHANNEL_ID, f"⬆️ {interaction.user.mention} upgraded **{PLANS[old_key]['label']}** → **{PLANS[new_key]['label']}**. Expires {discord_ts(expiry, 'F')}.")
    await send_dm(uid, f"⬆️ Your plan is now **{PLANS[new_key]['label']}** and expires {discord_ts(expiry, 'F')}.")

# Admin group: all operations are administrator-only.
admin_group = app_commands.Group(name="admin", description="Admin-only member, invite and plan controls", guild_ids=[GUILD_ID])
admin_group = app_commands.default_permissions(administrator=True)(admin_group)


def admin_only(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and interaction.guild.id == GUILD_ID and interaction.user.guild_permissions.administrator)


async def deny_admin(interaction: discord.Interaction) -> bool:
    if not admin_only(interaction):
        await interaction.response.send_message("Administrator permission is required, and this command only works in the configured server.", ephemeral=True)
        return True
    return False


@admin_group.command(name="info", description="View a member's invite and plan details")
@app_commands.describe(member="Member to inspect")
async def admin_info(interaction: discord.Interaction, member: discord.Member) -> None:
    if await deny_admin(interaction): return
    stats = get_stats(member.id)
    p = stats["plan"]
    text = (f"**Member:** {member.mention} (`{member.id}`)\n**Account created:** {discord_ts(member.created_at, 'F')}\n**Invites:** earned {stats['earned']} · spent {stats['spent']} · available {stats['available']}\n"
            f"**Qualified joins:** {stats['qualified']} · **Flagged joins:** {stats['flagged']}\n")
    if p:
        expiry = from_iso(p["expires_at"])
        text += f"**Plan:** {PLANS.get(p['plan_key'], {}).get('label', p['plan_key'])}\n**Status:** {status_text(p)}\n**Uptime:** {plan_uptime(p)}\n**Expires:** {discord_ts(expiry, 'F')} ({discord_ts(expiry, 'R')})"
    else:
        text += "**Plan:** None"
    await interaction.response.send_message(text, ephemeral=True)


@admin_group.command(name="status", description="Manually set a member's plan status")
@app_commands.describe(member="Member", status="Online, Offline, Reconnect or Expired")
@app_commands.choices(status=STATUS_CHOICES)
async def admin_status(interaction: discord.Interaction, member: discord.Member, status: app_commands.Choice[str]) -> None:
    if await deny_admin(interaction): return
    with db_session() as conn:
        row = conn.execute("SELECT * FROM plans WHERE user_id=?", (member.id,)).fetchone()
        if not row:
            await interaction.response.send_message("This member has no plan record. Use `/admin setplan` first.", ephemeral=True)
            return
        new_status = status.value
        now = now_utc()
        current_expiry = from_iso(row["expires_at"])
        if (current_expiry and current_expiry <= now) or new_status == "expired":
            new_status = "expired"
        if new_status == "expired":
            conn.execute("UPDATE plans SET status=?, status_since=?, expires_at=?, warned_expiry=1 WHERE user_id=?", (new_status, to_iso(now), to_iso(now), member.id))
        else:
            conn.execute("UPDATE plans SET status=?, status_since=? WHERE user_id=?", (new_status, to_iso(now), member.id))
    await interaction.response.send_message(f"Set {member.mention}'s plan status to **{new_status.title()}**.", ephemeral=True)
    await send_dm(member.id, f"ℹ️ An admin changed your VorstyAFK plan status to **{new_status.title()}**.")


@admin_group.command(name="addinvites", description="Add invite credits to a member")
@app_commands.describe(member="Member", amount="Positive invite amount")
async def admin_addinvites(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 100000]) -> None:
    if await deny_admin(interaction): return
    balance = change_balance(member.id, int(amount), "add")
    await interaction.response.send_message(f"Added **{amount}** invite credits to {member.mention}. Available balance: **{balance}**.", ephemeral=True)


@admin_group.command(name="setinvites", description="Set a member's total earned invite credits")
@app_commands.describe(member="Member", amount="Total earned credits; cannot be below already-spent credits")
async def admin_setinvites(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 0, 100000]) -> None:
    if await deny_admin(interaction): return
    balance = change_balance(member.id, int(amount), "set")
    await interaction.response.send_message(f"Set {member.mention}'s earned credits. Available balance: **{balance}**.", ephemeral=True)


@admin_group.command(name="removeinvites", description="Remove available invite credits from a member")
@app_commands.describe(member="Member", amount="Credits to remove")
async def admin_removeinvites(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 100000]) -> None:
    if await deny_admin(interaction): return
    balance = change_balance(member.id, int(amount), "remove")
    await interaction.response.send_message(f"Removed up to **{amount}** credits from {member.mention}. Available balance: **{balance}**.", ephemeral=True)


@admin_group.command(name="setplan", description="Grant or replace a member's plan")
@app_commands.describe(member="Member", plan="Plan to grant")
@app_commands.choices(plan=PLAN_CHOICES)
async def admin_setplan(interaction: discord.Interaction, member: discord.Member, plan: app_commands.Choice[str]) -> None:
    if await deny_admin(interaction): return
    expiry = activate_plan(member.id, plan.value, 0, admin_grant=True)
    await interaction.response.send_message(f"Granted **{PLANS[plan.value]['label']}** to {member.mention}. Status is **Offline** until set. Expires {discord_ts(expiry, 'F')}.", ephemeral=True)
    await send_dm(member.id, f"✅ An admin granted you **{PLANS[plan.value]['label']}**. Expires {discord_ts(expiry, 'F')}. Your status starts Offline.")


@admin_group.command(name="extend", description="Extend a member's plan expiry by days")
@app_commands.describe(member="Member", days="Number of days to add")
async def admin_extend(interaction: discord.Interaction, member: discord.Member, days: app_commands.Range[int, 1, 3650]) -> None:
    if await deny_admin(interaction): return
    with db_session() as conn:
        row = conn.execute("SELECT * FROM plans WHERE user_id=?", (member.id,)).fetchone()
        if not row:
            await interaction.response.send_message("This member has no plan record. Use `/admin setplan` first.", ephemeral=True)
            return
        old_expiry = from_iso(row["expires_at"]) or now_utc()
        base = max(old_expiry, now_utc())
        new_expiry = base + timedelta(days=int(days))
        conn.execute("UPDATE plans SET expires_at=?, warned_expiry=0 WHERE user_id=?", (to_iso(new_expiry), member.id))
    await interaction.response.send_message(f"Extended {member.mention}'s plan by **{days} days**. New expiry: {discord_ts(new_expiry, 'F')}.", ephemeral=True)
    await send_dm(member.id, f"ℹ️ An admin extended your VorstyAFK plan by **{days} days**. New expiry: {discord_ts(new_expiry, 'F')}.")


@admin_group.command(name="clearplan", description="Remove a member's plan record")
@app_commands.describe(member="Member")
async def admin_clearplan(interaction: discord.Interaction, member: discord.Member) -> None:
    if await deny_admin(interaction): return
    with db_session() as conn:
        conn.execute("DELETE FROM plans WHERE user_id=?", (member.id,))
    await interaction.response.send_message(f"Removed the plan record for {member.mention}. Invite balance was unchanged.", ephemeral=True)
    await send_dm(member.id, "ℹ️ An admin removed your VorstyAFK plan record.")


bot.tree.add_command(admin_group)

# ---------------- Render health server ----------------
web_app = Flask("vorstyafk")

@web_app.get("/")
def health():
    return jsonify(service="VorstyAFK", status="online" if bot.is_ready() else "starting", bot_ready=bot.is_ready(), guild_id=GUILD_ID), 200

@web_app.get("/health")
def health_check():
    return jsonify(ok=True, bot_ready=bot.is_ready()), 200


def start_web_server() -> None:
    Thread(target=lambda: web_app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True), name="render-health-server", daemon=True).start()


if __name__ == "__main__":
    init_db()
    start_web_server()
    if not TOKEN:
        raise RuntimeError("Missing Discord token. Set DISCORD_TOKEN in Render Environment.")
    bot.run(TOKEN, log_handler=None)
