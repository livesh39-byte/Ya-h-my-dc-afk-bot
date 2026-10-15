import os
import sqlite3
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "vorstyafk.sqlite3")
ALLOWED_GUILD_ID = int(os.getenv("ALLOWED_GUILD_ID", "0") or 0)
EXPIRE_ALERT_CHANNEL_ID = int(os.getenv("EXPIRE_ALERT_CHANNEL_ID", "0") or 0)
UPGRADE_ALERT_CHANNEL_ID = int(os.getenv("UPGRADE_ALERT_CHANNEL_ID", "0") or 0)
PREFIX = "$"
DAILY_REWARD = 1  # bonus invite credits, once every 24 hours
DAILY_COOLDOWN = 24 * 60 * 60
MIN_ACCOUNT_AGE_DAYS = 7

PLAN_DAYS = {2: 7, 4: 14, 8: 23, 12: 34, 16: 60, 24: 365}
PLAN_CHOICES = [
    app_commands.Choice(name=f"{n} invites — {days} days", value=n)
    for n, days in PLAN_DAYS.items()
]

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("VorstyAFK")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.invites = True
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

invite_cache: dict[int, dict[str, int]] = {}
_db_lock = asyncio.Lock()


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def fmt_ts(ts: int) -> str:
    return f"<t:{int(ts)}:F>"


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def db_init():
    with connect_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS user_stats (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            total_invites INTEGER NOT NULL DEFAULT 0,
            balance INTEGER NOT NULL DEFAULT 0,
            spent INTEGER NOT NULL DEFAULT 0,
            daily_last INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            invite_cost INTEGER NOT NULL,
            days INTEGER NOT NULL,
            activated_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            reminded INTEGER NOT NULL DEFAULT 0,
            expired_notified INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_plans_user ON plans(guild_id, user_id, expires_at);
        CREATE TABLE IF NOT EXISTS join_records (
            guild_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            inviter_id INTEGER NOT NULL,
            joined_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, member_id)
        );
        """)


def ensure_user(guild_id: int, user_id: int):
    with connect_db() as db:
        db.execute("INSERT OR IGNORE INTO user_stats(guild_id,user_id) VALUES(?,?)", (guild_id, user_id))


def get_stats(guild_id: int, user_id: int):
    ensure_user(guild_id, user_id)
    with connect_db() as db:
        return db.execute("SELECT * FROM user_stats WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()


def active_plan(guild_id: int, user_id: int):
    ts = now_ts()
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM plans WHERE guild_id=? AND user_id=? AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (guild_id, user_id, ts),
        ).fetchone()


def make_embed(title: str, description: str = "", color: discord.Color = discord.Color.blurple()):
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="VorstyAFK • Invite & Plan System")
    return e


async def alert_channel(channel_id: int) -> Optional[discord.abc.Messageable]:
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Cannot access configured alert channel %s", channel_id)
            return None
    return channel


async def notify_plan_activation(guild: discord.Guild, user: discord.abc.User, cost: int, days: int, expires_at: int):
    channel = await alert_channel(UPGRADE_ALERT_CHANNEL_ID)
    if channel:
        e = make_embed("✅ Plan Activated", f"**User:** {user.mention}\n**Plan:** `{cost} invites / {days} days`\n**Expires:** {fmt_ts(expires_at)}", discord.Color.green())
        try:
            await channel.send(embed=e, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            log.exception("Failed sending activation alert")


async def claim_plan(interaction: discord.Interaction, invite_cost: int, command_label: str):
    if interaction.guild is None:
        return await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
    if ALLOWED_GUILD_ID and interaction.guild.id != ALLOWED_GUILD_ID:
        return await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
    days = PLAN_DAYS.get(invite_cost)
    if days is None:
        return await interaction.response.send_message("Invalid plan selected.", ephemeral=True)

    async with _db_lock:
        stats = get_stats(interaction.guild.id, interaction.user.id)
        current = active_plan(interaction.guild.id, interaction.user.id)
        if current:
            return await interaction.response.send_message(
                f"❌ You already have an active plan until {fmt_ts(current['expires_at'])}. You can claim another plan after it expires.",
                ephemeral=True,
            )
        if stats["balance"] < invite_cost:
            return await interaction.response.send_message(
                f"❌ You need **{invite_cost} available invites** for this plan. Your balance is **{stats['balance']}**.",
                ephemeral=True,
            )
        activated = now_ts()
        expires = activated + days * 86400
        with connect_db() as db:
            db.execute("UPDATE user_stats SET balance=balance-?, spent=spent+? WHERE guild_id=? AND user_id=?",
                       (invite_cost, invite_cost, interaction.guild.id, interaction.user.id))
            db.execute("INSERT INTO plans(guild_id,user_id,invite_cost,days,activated_at,expires_at) VALUES(?,?,?,?,?,?)",
                       (interaction.guild.id, interaction.user.id, invite_cost, days, activated, expires))

    e = make_embed("🎉 Plan Activated!", f"**Plan:** `{invite_cost} invites → {days} days`\n**Invites spent:** `{invite_cost}`\n**Expires:** {fmt_ts(expires)}\n\nYour plan is now active.", discord.Color.green())
    await interaction.response.send_message(embed=e, ephemeral=True)
    await notify_plan_activation(interaction.guild, interaction.user, invite_cost, days, expires)


@bot.event
async def on_ready():
    db_init()
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {i.code: (i.uses or 0) for i in invites}
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not cache invites for guild %s; check Manage Server permission", guild.id)
    try:
        if ALLOWED_GUILD_ID:
            guild_obj = discord.Object(id=ALLOWED_GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            log.info("Synced %s slash commands to configured guild", len(synced))
        else:
            synced = await bot.tree.sync()
            log.info("Synced %s global slash commands", len(synced))
    except Exception:
        log.exception("Slash command sync failed")
    if not expiry_loop.is_running():
        expiry_loop.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {i.code: (i.uses or 0) for i in invites}
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_invite_create(invite: discord.Invite):
    if invite.guild:
        invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0


@bot.event
async def on_invite_delete(invite: discord.Invite):
    if invite.guild:
        invite_cache.setdefault(invite.guild.id, {}).pop(invite.code, None)


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    guild = member.guild
    try:
        current_invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        log.warning("Cannot inspect invites for %s; grant Manage Server permission", guild.id)
        return

    old_uses = invite_cache.get(guild.id, {})
    used_invite = None
    for inv in current_invites:
        if (inv.uses or 0) > old_uses.get(inv.code, 0):
            used_invite = inv
            break
    invite_cache[guild.id] = {i.code: (i.uses or 0) for i in current_invites}
    if not used_invite or not used_invite.inviter or used_invite.inviter.id == member.id:
        return

    # Anti-abuse: skip very new accounts, bots, and members who have already earned an invite credit.
    account_age = datetime.now(timezone.utc) - member.created_at
    if account_age < timedelta(days=MIN_ACCOUNT_AGE_DAYS):
        log.info("Ignored invite credit for young account %s", member.id)
        return

    inviter_id = used_invite.inviter.id
    with connect_db() as db:
        try:
            db.execute("INSERT INTO join_records(guild_id,member_id,inviter_id,joined_at) VALUES(?,?,?,?)",
                       (guild.id, member.id, inviter_id, now_ts()))
        except sqlite3.IntegrityError:
            return  # rejoining member cannot generate duplicate invite credit
        db.execute("INSERT OR IGNORE INTO user_stats(guild_id,user_id) VALUES(?,?)", (guild.id, inviter_id))
        db.execute("UPDATE user_stats SET total_invites=total_invites+1, balance=balance+1 WHERE guild_id=? AND user_id=?",
                   (guild.id, inviter_id))

    try:
        await used_invite.inviter.send(
            embed=make_embed("➕ Invite Credited", f"Your invite was counted in **{guild.name}**.\n**+1 invite credit** added to your balance.", discord.Color.green())
        )
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.tree.command(name="help", description="Show VorstyAFK invite and plan commands")
async def help_slash(interaction: discord.Interaction):
    e = make_embed("VorstyAFK Commands", "**Invite & plans**\n`/i` — Invite stats\n`/balance` — Available invite credits\n`/leaderboard` — Top inviters\n`/daily` — Daily invite bonus\n`/claim` — Claim a plan\n`/upgrade` — Purchase a plan\n`/myplan` — Current plan status\n\n**Admin**\n`/adminstats` — Server plan/invite overview (admins only)\n`$i` — Prefix version of `/i`\n\nPlans: 2=7d, 4=14d, 8=23d, 12=34d, 16=60d, 24=365d")
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="claim", description="Claim a plan using available invite credits")
@app_commands.choices(invites=PLAN_CHOICES)
async def claim(interaction: discord.Interaction, invites: app_commands.Choice[int]):
    await claim_plan(interaction, invites.value, "claim")


@bot.tree.command(name="upgrade", description="Use invite credits to activate a plan")
@app_commands.choices(invites=PLAN_CHOICES)
async def upgrade(interaction: discord.Interaction, invites: app_commands.Choice[int]):
    if interaction.guild is None:
        return await interaction.response.send_message("This command only works inside a server.", ephemeral=True)
    stats = get_stats(interaction.guild.id, interaction.user.id)
    if stats["balance"] < 2:
        return await interaction.response.send_message("❌ `/upgrade` requires at least **2 available invite credits**.", ephemeral=True)
    await claim_plan(interaction, invites.value, "upgrade")


@bot.tree.command(name="i", description="Show your invite stats")
async def invite_info(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if interaction.guild is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    target = member or interaction.user
    s = get_stats(interaction.guild.id, target.id)
    e = make_embed(f"📨 Invite Info • {target.display_name}",
                   f"**Successful invites:** `{s['total_invites']}`\n**Available invite credits:** `{s['balance']}`\n**Credits spent:** `{s['spent']}`")
    e.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.command(name="i")
@commands.guild_only()
async def invite_info_prefix(ctx: commands.Context, member: Optional[discord.Member] = None):
    target = member or ctx.author
    s = get_stats(ctx.guild.id, target.id)
    e = make_embed(f"📨 Invite Info • {target.display_name}",
                   f"**Successful invites:** `{s['total_invites']}`\n**Available invite credits:** `{s['balance']}`\n**Credits spent:** `{s['spent']}`")
    e.set_thumbnail(url=target.display_avatar.url)
    await ctx.reply(embed=e, mention_author=False)


@bot.tree.command(name="balance", description="Show available invite credits")
async def balance(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    s = get_stats(interaction.guild.id, interaction.user.id)
    e = make_embed("💳 Invite Balance", f"**Available:** `{s['balance']}`\n**Successful invites:** `{s['total_invites']}`\n**Spent on plans:** `{s['spent']}`\n**Daily reward:** `+{DAILY_REWARD} credit every 24 hours`")
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="leaderboard", description="Show the top 10 inviters")
async def leaderboard(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    with connect_db() as db:
        rows = db.execute("SELECT user_id,total_invites,balance FROM user_stats WHERE guild_id=? ORDER BY total_invites DESC, balance DESC LIMIT 10", (interaction.guild.id,)).fetchall()
    if not rows:
        return await interaction.response.send_message(embed=make_embed("🏆 Invite Leaderboard", "No invite data yet."), ephemeral=True)
    lines = []
    for idx, row in enumerate(rows, 1):
        lines.append(f"**{idx}.** <@{row['user_id']}> — **{row['total_invites']}** invites · `{row['balance']}` credits")
    await interaction.response.send_message(embed=make_embed("🏆 Invite Leaderboard", "\n".join(lines)), ephemeral=False)


@bot.tree.command(name="daily", description="Claim one daily invite credit (24-hour cooldown)")
async def daily(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    async with _db_lock:
        s = get_stats(interaction.guild.id, interaction.user.id)
        now = now_ts()
        next_at = int(s["daily_last"]) + DAILY_COOLDOWN
        if now < next_at:
            return await interaction.response.send_message(f"⏳ You already claimed daily. Try again <t:{next_at}:R>.", ephemeral=True)
        with connect_db() as db:
            db.execute("UPDATE user_stats SET balance=balance+?, daily_last=? WHERE guild_id=? AND user_id=?",
                       (DAILY_REWARD, now, interaction.guild.id, interaction.user.id))
    await interaction.response.send_message(embed=make_embed("🎁 Daily Reward Claimed", f"You received **+{DAILY_REWARD} invite credit**. Use `/balance` to check your balance.", discord.Color.green()), ephemeral=True)


@bot.tree.command(name="myplan", description="Show your current plan and expiry")
async def myplan(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    p = active_plan(interaction.guild.id, interaction.user.id)
    if not p:
        return await interaction.response.send_message(embed=make_embed("📅 My Plan", "You don't have an active plan. Use `/claim` or `/upgrade` to activate one."), ephemeral=True)
    remaining = max(0, p["expires_at"] - now_ts())
    days_left = remaining // 86400
    hours_left = (remaining % 86400) // 3600
    e = make_embed("📅 Your Active Plan", f"**Plan:** `{p['invite_cost']} invites / {p['days']} days`\n**Activated:** {fmt_ts(p['activated_at'])}\n**Expires:** {fmt_ts(p['expires_at'])}\n**Time left:** `{days_left}d {hours_left}h`")
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="adminstats", description="Admin-only invite and plan statistics")
@app_commands.default_permissions(administrator=True)
async def adminstats(interaction: discord.Interaction):
    if interaction.guild is None or not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("⛔ Administrator permission required.", ephemeral=True)
    ts = now_ts()
    with connect_db() as db:
        users = db.execute("SELECT COUNT(*) AS n FROM user_stats WHERE guild_id=?", (interaction.guild.id,)).fetchone()["n"]
        invites = db.execute("SELECT COALESCE(SUM(total_invites),0) AS n FROM user_stats WHERE guild_id=?", (interaction.guild.id,)).fetchone()["n"]
        balance = db.execute("SELECT COALESCE(SUM(balance),0) AS n FROM user_stats WHERE guild_id=?", (interaction.guild.id,)).fetchone()["n"]
        spent = db.execute("SELECT COALESCE(SUM(spent),0) AS n FROM user_stats WHERE guild_id=?", (interaction.guild.id,)).fetchone()["n"]
        active = db.execute("SELECT COUNT(*) AS n FROM plans WHERE guild_id=? AND expires_at>?", (interaction.guild.id, ts)).fetchone()["n"]
        expired = db.execute("SELECT COUNT(*) AS n FROM plans WHERE guild_id=? AND expires_at<=?", (interaction.guild.id, ts)).fetchone()["n"]
    e = make_embed("🛠️ VorstyAFK Admin Dashboard", f"**Tracked users:** `{users}`\n**Successful invites:** `{invites}`\n**Available credits:** `{balance}`\n**Credits spent:** `{spent}`\n**Active plans:** `{active}`\n**Expired plans:** `{expired}`", discord.Color.gold())
    await interaction.response.send_message(embed=e, ephemeral=True)


@tasks.loop(minutes=5)
async def expiry_loop():
    now = now_ts()
    # Read a batch; update flags before sending to avoid duplicate notifications after restarts.
    with connect_db() as db:
        reminders = db.execute("SELECT * FROM plans WHERE expires_at>? AND expires_at<=? AND reminded=0", (now, now + 86400)).fetchall()
        expired = db.execute("SELECT * FROM plans WHERE expires_at<=? AND expired_notified=0", (now,)).fetchall()
        for row in reminders:
            db.execute("UPDATE plans SET reminded=1 WHERE id=?", (row["id"],))
        for row in expired:
            db.execute("UPDATE plans SET expired_notified=1 WHERE id=?", (row["id"],))

    for row in reminders:
        user = bot.get_user(row["user_id"])
        if user is None:
            try:
                user = await bot.fetch_user(row["user_id"])
            except (discord.NotFound, discord.HTTPException):
                user = None
        if user:
            try:
                await user.send(embed=make_embed("⏰ Plan Expiring Soon", f"Your `{row['invite_cost']} invites / {row['days']} days` plan expires {fmt_ts(row['expires_at'])}.", discord.Color.orange()))
            except (discord.Forbidden, discord.HTTPException):
                pass

    for row in expired:
        user = bot.get_user(row["user_id"])
        if user is None:
            try:
                user = await bot.fetch_user(row["user_id"])
            except (discord.NotFound, discord.HTTPException):
                user = None
        if user:
            try:
                await user.send(embed=make_embed("⌛ Plan Expired", f"Your `{row['invite_cost']} invites / {row['days']} days` plan has expired. You can activate a new plan with `/claim`.", discord.Color.red()))
            except (discord.Forbidden, discord.HTTPException):
                pass
        channel = await alert_channel(EXPIRE_ALERT_CHANNEL_ID)
        if channel:
            try:
                await channel.send(embed=make_embed("⌛ Plan Expired", f"**User ID:** `{row['user_id']}`\n**Plan:** `{row['invite_cost']} invites / {row['days']} days`\n**Expired:** {fmt_ts(row['expires_at'])}", discord.Color.red()))
            except discord.HTTPException:
                log.exception("Failed sending expiry channel alert")


@expiry_loop.before_loop
async def before_expiry_loop():
    await bot.wait_until_ready()


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NoPrivateMessage):
        return await ctx.reply("This command only works inside a server.", mention_author=False)
    log.exception("Prefix command error", exc_info=error)
    try:
        await ctx.reply("Something went wrong. Check Render logs for details.", mention_author=False)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable is missing")
    db_init()
    bot.run(TOKEN)
