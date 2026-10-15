import os
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread, Lock

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask

# ---------- CONFIG ----------
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
# These defaults follow the IDs from your corrected setup. You can override them in Render Environment.
ALLOWED_GUILD_ID = int(os.getenv("ALLOWED_GUILD_ID", "1559826100153090080"))
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID", "1559831974162473050"))
DB_FILE = os.getenv("DB_FILE", "database.json")
PREFIX = "$"

# invite cost -> plan duration
PLAN_DAYS = {2: 7, 4: 14, 8: 23, 12: 34, 16: 60, 24: 365}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("VorstyAFK")

# ---------- KEEP-ALIVE WEB SERVER (Render) ----------
web = Flask(__name__)

@web.get("/")
def home():
    return "VorstyAFK bot is running.", 200

def run_web():
    port = int(os.getenv("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, use_reloader=False)

Thread(target=run_web, daemon=True).start()

# ---------- DATABASE ----------
_db_lock = Lock()

def fresh_db():
    return {"users": {}, "invite_codes": {}, "upgrade_requests": []}

def load_db():
    if not os.path.exists(DB_FILE):
        return fresh_db()
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return fresh_db()
        data.setdefault("users", {})
        data.setdefault("invite_codes", {})
        data.setdefault("upgrade_requests", [])
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read database (%s); starting with an empty database.", exc)
        return fresh_db()

db = load_db()

def save_db():
    with _db_lock:
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DB_FILE)

def user_data(user_id):
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "invites": 0,
            "claimed_invites": 0,
            "plan_invites": 0,
            "plan_days": 0,
            "expires_at": None,
            "server_ip": None,
        }
    entry = db["users"][uid]
    # Fill missing keys when loading an older database.
    defaults = {"invites": 0, "claimed_invites": 0, "plan_invites": 0, "plan_days": 0, "expires_at": None, "server_ip": None}
    for key, value in defaults.items():
        entry.setdefault(key, value)
    return entry

def parse_expiry(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None

def plan_active(data):
    expiry = parse_expiry(data.get("expires_at"))
    return bool(expiry and expiry > datetime.now(timezone.utc))

def expire_user_plan(data):
    expiry = parse_expiry(data.get("expires_at"))
    if expiry and expiry <= datetime.now(timezone.utc):
        data["plan_invites"] = 0
        data["plan_days"] = 0
        data["expires_at"] = None
        data["server_ip"] = None
        save_db()
        return True
    return False

def format_expiry(value):
    expiry = parse_expiry(value)
    if not expiry:
        return "No active plan"
    unix = int(expiry.timestamp())
    return f"<t:{unix}:F> (<t:{unix}:R>)"

# ---------- DISCORD BOT ----------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.invites = True
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
invite_cache = {}


def allowed_guild(guild):
    return guild is not None and guild.id == ALLOWED_GUILD_ID

async def send_admin(message):
    if not ADMIN_CHANNEL_ID:
        return
    channel = bot.get_channel(ADMIN_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ADMIN_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Admin channel %s could not be accessed.", ADMIN_CHANNEL_ID)
            return
    try:
        await channel.send(message)
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Could not send admin notification: %s", exc)

async def get_invites(guild):
    try:
        return await guild.invites()
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Cannot read invites in %s: %s", guild.id, exc)
        return []

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    guild = bot.get_guild(ALLOWED_GUILD_ID)
    if guild:
        invite_cache[guild.id] = await get_invites(guild)
        try:
            synced = await bot.tree.sync(guild=discord.Object(id=ALLOWED_GUILD_ID))
            log.info("Synced %s guild slash commands to %s", len(synced), guild.name)
        except discord.HTTPException as exc:
            log.exception("Slash command sync failed: %s", exc)
    else:
        log.warning("Configured guild %s is not visible to this bot. Check ALLOWED_GUILD_ID and bot invite permissions.", ALLOWED_GUILD_ID)
    if not expire_plans.is_running():
        expire_plans.start()

@bot.event
async def on_invite_create(invite):
    if invite.guild and allowed_guild(invite.guild):
        invite_cache[invite.guild.id] = await get_invites(invite.guild)

@bot.event
async def on_invite_delete(invite):
    if invite.guild and allowed_guild(invite.guild):
        invite_cache[invite.guild.id] = await get_invites(invite.guild)

@bot.event
async def on_member_join(member):
    if not allowed_guild(member.guild):
        return
    old_invites = invite_cache.get(member.guild.id, [])
    new_invites = await get_invites(member.guild)
    invite_cache[member.guild.id] = new_invites
    used_invite = None
    for invite in new_invites:
        old = next((item for item in old_invites if item.code == invite.code), None)
        old_uses = old.uses if old and old.uses is not None else 0
        new_uses = invite.uses if invite.uses is not None else 0
        if new_uses > old_uses:
            used_invite = invite
            break
    if used_invite and used_invite.inviter and used_invite.inviter.id != member.id:
        inviter_data = user_data(used_invite.inviter.id)
        inviter_data["invites"] = int(inviter_data.get("invites", 0)) + 1
        db["invite_codes"][used_invite.code] = {
            "inviter_id": used_invite.inviter.id,
            "last_joined_id": member.id,
            "uses": used_invite.uses,
        }
        save_db()
        await send_admin(f"📨 **Invite tracked**\nInviter: {used_invite.inviter.mention} (`{used_invite.inviter.id}`)\nNew member: {member.mention} (`{member.id}`)\nTotal invites: **{inviter_data['invites']}**")

@tasks.loop(minutes=1)
async def expire_plans():
    changed = False
    now = datetime.now(timezone.utc)
    for entry in db["users"].values():
        expiry = parse_expiry(entry.get("expires_at"))
        if expiry and expiry <= now:
            entry["plan_invites"] = 0
            entry["plan_days"] = 0
            entry["expires_at"] = None
            entry["server_ip"] = None
            changed = True
    if changed:
        save_db()

@expire_plans.before_loop
async def before_expire_plans():
    await bot.wait_until_ready()

# ---------- SLASH COMMANDS (only these 3 are registered) ----------
@bot.tree.command(name="help", description="Show VorstyAFK bot commands")
@app_commands.guild_only()
async def slash_help(interaction: discord.Interaction):
    if not allowed_guild(interaction.guild):
        await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
        return
    embed = discord.Embed(title="VorstyAFK • Help", description="Invite rewards and plan management", color=discord.Color.blurple())
    embed.add_field(name="/claim", value="Claim a plan with your invites. Example: `/claim invite:4 ip:play.example.net`", inline=False)
    embed.add_field(name="/checkinvits", value="Check your tracked invites, used invites, and active plan.", inline=False)
    embed.add_field(name="Plan tiers", value="2 invites → 7 days\n4 invites → 14 days\n8 invites → 23 days\n12 invites → 34 days\n16 invites → 60 days\n24 invites → 365 days", inline=False)
    embed.set_footer(text="A user can have only one active plan at a time.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def claim_plan(user: discord.abc.User, invite_count: int, server_ip: str):
    if invite_count not in PLAN_DAYS:
        return False, "Choose one of these invite plans: **2, 4, 8, 12, 16, or 24**."
    if not server_ip.strip() or len(server_ip) > 150:
        return False, "Enter a valid server IP/address (up to 150 characters)."
    data = user_data(user.id)
    if plan_active(data):
        return False, f"You already have an active plan until {format_expiry(data.get('expires_at'))}."
    # Clear an expired plan before checking / claiming.
    expire_user_plan(data)
    available = int(data.get("invites", 0))
    if available < invite_count:
        return False, f"You need **{invite_count} invites**, but only **{available}** are tracked for you."
    days = PLAN_DAYS[invite_count]
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    data["invites"] = available - invite_count
    data["claimed_invites"] = int(data.get("claimed_invites", 0)) + invite_count
    data["plan_invites"] = invite_count
    data["plan_days"] = days
    data["expires_at"] = expiry.isoformat()
    data["server_ip"] = server_ip.strip()
    save_db()
    return True, f"Plan activated for **{days} days**!\nServer: `{server_ip.strip()}`\nExpires: {format_expiry(data['expires_at'])}\nRemaining invites: **{data['invites']}**."

@bot.tree.command(name="claim", description="Claim a plan using your invites")
@app_commands.guild_only()
@app_commands.describe(invite="Invite count plan: 2, 4, 8, 12, 16, or 24", ip="Your Minecraft server IP/address")
@app_commands.choices(invite=[
    app_commands.Choice(name="2 invites — 7 days", value=2),
    app_commands.Choice(name="4 invites — 14 days", value=4),
    app_commands.Choice(name="8 invites — 23 days", value=8),
    app_commands.Choice(name="12 invites — 34 days", value=12),
    app_commands.Choice(name="16 invites — 60 days", value=16),
    app_commands.Choice(name="24 invites — 365 days", value=24),
])
async def slash_claim(interaction: discord.Interaction, invite: app_commands.Choice[int], ip: str):
    if not allowed_guild(interaction.guild):
        await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
        return
    ok, message = await claim_plan(interaction.user, invite.value, ip)
    await interaction.response.send_message(("✅ " if ok else "❌ ") + message, ephemeral=True)
    if ok:
        await send_admin(f"🎟️ **Plan claimed**\nUser: {interaction.user.mention} (`{interaction.user.id}`)\nPlan: **{invite.value} invites / {PLAN_DAYS[invite.value]} days**\nServer: `{ip.strip()}`\nExpires: {format_expiry(user_data(interaction.user.id).get('expires_at'))}")

@bot.tree.command(name="checkinvits", description="Check your invite count and active plan")
@app_commands.guild_only()
async def slash_checkinvits(interaction: discord.Interaction):
    if not allowed_guild(interaction.guild):
        await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
        return
    data = user_data(interaction.user.id)
    expire_user_plan(data)
    active = plan_active(data)
    embed = discord.Embed(title="Your VorstyAFK Stats", color=discord.Color.green())
    embed.add_field(name="Available invites", value=str(data.get("invites", 0)), inline=True)
    embed.add_field(name="Invites used for claims", value=str(data.get("claimed_invites", 0)), inline=True)
    embed.add_field(name="Active plan", value=(f"{data.get('plan_days', 0)} days" if active else "None"), inline=True)
    embed.add_field(name="Expires", value=(format_expiry(data.get("expires_at")) if active else "No active plan"), inline=False)
    embed.add_field(name="Server", value=(f"`{data.get('server_ip')}`" if active and data.get("server_ip") else "Not set"), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- PREFIX COMMANDS (legacy/admin commands retained) ----------
@bot.command(name="help")
async def prefix_help(ctx):
    embed = discord.Embed(title="VorstyAFK • Help", color=discord.Color.blurple())
    embed.description = "**Slash:** `/help`, `/claim invite: ip:`, `/checkinvits`\n**Prefix:** `$claim <count> ip: <server>`, `$check invite`, `$checkinvites`, `$upgrade`\n**Admin:** `$setinvite @user amount`, `$userstatus @user`, `$dm @user message`"
    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="claim")
async def prefix_claim(ctx, invite_count: int, *, ip: str):
    if not allowed_guild(ctx.guild):
        return await ctx.reply("This bot is not configured for this server.")
    server_ip = ip.strip()
    if server_ip.lower().startswith("ip:"):
        server_ip = server_ip[3:].strip()
    ok, message = await claim_plan(ctx.author, invite_count, server_ip)
    await ctx.reply(("✅ " if ok else "❌ ") + message, mention_author=False)
    if ok:
        await send_admin(f"🎟️ **Plan claimed**\nUser: {ctx.author.mention} (`{ctx.author.id}`)\nPlan: **{invite_count} invites / {PLAN_DAYS[invite_count]} days**\nServer: `{server_ip}`")

@bot.group(name="check", invoke_without_command=True)
async def prefix_check(ctx):
    await ctx.reply("Use `$check invite` or `$checkinvites`.", mention_author=False)

@prefix_check.command(name="invite")
async def prefix_check_invite(ctx):
    data = user_data(ctx.author.id)
    expire_user_plan(data)
    active = plan_active(data)
    await ctx.reply(f"📨 Available invites: **{data.get('invites', 0)}**\n🎟️ Claimed invites: **{data.get('claimed_invites', 0)}**\n📦 Active plan: **{data.get('plan_days', 0) if active else 0} days**\n⏳ Expires: {format_expiry(data.get('expires_at')) if active else 'No active plan'}", mention_author=False)

@bot.command(name="checkinvites", aliases=["checkinvits"])
async def prefix_checkinvites(ctx):
    data = user_data(ctx.author.id)
    expire_user_plan(data)
    await ctx.invoke(prefix_check_invite)

async def require_admin(ctx):
    if not ctx.guild or not (ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator):
        await ctx.reply("❌ You need **Manage Server** permission to use this command.", mention_author=False)
        return False
    return True

@bot.command(name="setinvite")
async def setinvite(ctx, member: discord.Member, amount: int):
    if not await require_admin(ctx):
        return
    if amount < 0:
        return await ctx.reply("Amount cannot be negative.", mention_author=False)
    data = user_data(member.id)
    data["invites"] = amount
    save_db()
    await ctx.reply(f"✅ Set {member.mention}'s available invites to **{amount}**.", mention_author=False)
    await send_admin(f"🛠️ {ctx.author.mention} set {member.mention}'s available invites to **{amount}**.")

@bot.command(name="userstatus")
async def userstatus(ctx, member: discord.Member):
    if not await require_admin(ctx):
        return
    data = user_data(member.id)
    expire_user_plan(data)
    active = plan_active(data)
    await ctx.reply(f"**{member}** (`{member.id}`)\nAvailable invites: **{data.get('invites', 0)}**\nClaimed invites: **{data.get('claimed_invites', 0)}**\nActive plan: **{data.get('plan_days', 0) if active else 0} days**\nServer: `{data.get('server_ip') or 'Not set'}`\nExpires: {format_expiry(data.get('expires_at')) if active else 'No active plan'}", mention_author=False)

@bot.command(name="dm")
async def dm_user(ctx, member: discord.Member, *, message: str):
    if not await require_admin(ctx):
        return
    try:
        await member.send(message)
        await ctx.reply(f"✅ DM sent to {member.mention}.", mention_author=False)
    except discord.Forbidden:
        await ctx.reply("❌ I can't DM that user; their privacy settings may block it.", mention_author=False)

@bot.command(name="upgrade")
async def upgrade(ctx, *, details: str = ""):
    data = user_data(ctx.author.id)
    request = {"user_id": ctx.author.id, "details": details[:500], "created_at": datetime.now(timezone.utc).isoformat()}
    db["upgrade_requests"].append(request)
    save_db()
    await ctx.reply("✅ Upgrade request sent to the admins.", mention_author=False)
    await send_admin(f"⬆️ **Upgrade request**\nUser: {ctx.author.mention} (`{ctx.author.id}`)\nCurrent plan: {data.get('plan_days', 0)} days\nDetails: {details or 'No details provided'}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Missing argument: `{error.param.name}`. Use `$help` for examples.", mention_author=False)
        return
    if isinstance(error, commands.BadArgument):
        await ctx.reply("Invalid argument. Check the user mention/number and try again.", mention_author=False)
        return
    log.error("Prefix command error: %s", error, exc_info=error)
    await ctx.reply("Something went wrong while running that command. Check the Render logs.", mention_author=False)

async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Add your bot token in Render → Environment.")
    async with bot:
        await bot.start(TOKEN, reconnect=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Bot stopped during startup/runtime.")
        raise
