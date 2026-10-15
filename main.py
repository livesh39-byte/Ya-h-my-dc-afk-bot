import os
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread, RLock

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask

# ==================== CONFIG ====================
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ALLOWED_GUILD_ID = int(os.getenv("ALLOWED_GUILD_ID", "1559826100153090080"))
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID", "1559831974162473050"))
# Optional separate alert channels. If blank/0, alerts fall back to ADMIN_CHANNEL_ID.
EXPIRE_ALERT_CHANNEL_ID = int(os.getenv("EXPIRE_ALERT_CHANNEL_ID", "0") or "0")
UPGRADE_ALERT_CHANNEL_ID = int(os.getenv("UPGRADE_ALERT_CHANNEL_ID", "0") or "0")
DB_FILE = os.getenv("DB_FILE", "database.json")
PREFIX = "$"
PLAN_DAYS = {2: 7, 4: 14, 8: 23, 12: 34, 16: 60, 24: 365}
UPGRADE_COST = 2  # submitting an upgrade request costs 2 available invites

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("VorstyAFK")

# ==================== RENDER WEB ENDPOINT ====================
web = Flask(__name__)
@web.get("/")
def home():
    return "VorstyAFK is running.", 200

def run_web():
    port = int(os.getenv("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, use_reloader=False)
Thread(target=run_web, daemon=True).start()

# ==================== DATABASE ====================
_db_lock = RLock()
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
        log.warning("Could not read database: %s", exc)
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
        db["users"][uid] = {}
    entry = db["users"][uid]
    defaults = {
        "invites": 0, "claimed_invites": 0, "plan_invites": 0,
        "plan_days": 0, "expires_at": None, "server_ip": None,
        "expiry_notified_for": None,
    }
    for key, value in defaults.items():
        entry.setdefault(key, value)
    return entry

def parse_expiry(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None

def plan_active(data):
    expiry = parse_expiry(data.get("expires_at"))
    return bool(expiry and expiry > datetime.now(timezone.utc))

def format_expiry(value):
    expiry = parse_expiry(value)
    if not expiry:
        return "No active plan"
    stamp = int(expiry.timestamp())
    return f"<t:{stamp}:F> (<t:{stamp}:R>)"

def plan_list_text():
    return "\n".join(f"**{invites} invites** → **{days} days**" for invites, days in PLAN_DAYS.items())

# ==================== BOT ====================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.invites = True
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
invite_cache = {}

def allowed_guild(guild):
    return guild is not None and guild.id == ALLOWED_GUILD_ID

async def send_to_channel(channel_id, message):
    target_id = channel_id or ADMIN_CHANNEL_ID
    if not target_id:
        return
    channel = bot.get_channel(target_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(target_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Cannot access alert channel %s: %s", target_id, exc)
            return
    try:
        await channel.send(message, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Cannot send alert to channel %s: %s", target_id, exc)

async def send_admin(message):
    await send_to_channel(ADMIN_CHANNEL_ID, message)

async def get_invites(guild):
    try:
        return await guild.invites()
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Cannot read invites in guild %s: %s", guild.id, exc)
        return []

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    guild = bot.get_guild(ALLOWED_GUILD_ID)
    if guild:
        invite_cache[guild.id] = await get_invites(guild)
        try:
            # Copy global commands into this guild so they appear immediately in the configured server.
            target_guild = discord.Object(id=ALLOWED_GUILD_ID)
            bot.tree.copy_global_to(guild=target_guild)
            synced = await bot.tree.sync(guild=target_guild)
            log.info("Synced %s slash commands in %s", len(synced), guild.name)
        except discord.HTTPException as exc:
            log.exception("Slash command sync failed: %s", exc)
    else:
        log.warning("Configured guild %s not found. Check ALLOWED_GUILD_ID and bot server membership.", ALLOWED_GUILD_ID)
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
        old_uses = old.uses or 0 if old else 0
        new_uses = invite.uses or 0
        if new_uses > old_uses:
            used_invite = invite
            break
    if used_invite and used_invite.inviter and used_invite.inviter.id != member.id:
        data = user_data(used_invite.inviter.id)
        data["invites"] = int(data.get("invites", 0)) + 1
        db["invite_codes"][used_invite.code] = {
            "inviter_id": used_invite.inviter.id,
            "last_joined_id": member.id,
            "uses": used_invite.uses,
        }
        save_db()
        await send_admin(f"📨 **Invite tracked**\nInviter: {used_invite.inviter.mention} (`{used_invite.inviter.id}`)\nNew member: {member.mention} (`{member.id}`)\nAvailable invites: **{data['invites']}**")

# ==================== PLAN EXPIRY ALERTS ====================
@tasks.loop(minutes=1)
async def expire_plans():
    now = datetime.now(timezone.utc)
    changed = False
    for uid, data in list(db["users"].items()):
        expiry = parse_expiry(data.get("expires_at"))
        if not expiry or expiry > now:
            continue
        # Alert once for each expiry timestamp before clearing the plan.
        expiry_key = expiry.isoformat()
        if data.get("expiry_notified_for") != expiry_key:
            await send_to_channel(EXPIRE_ALERT_CHANNEL_ID, f"⏰ **Plan expired**\nUser: <@{uid}> (`{uid}`)\nServer: `{data.get('server_ip') or 'Not set'}`\nExpired: {format_expiry(data.get('expires_at'))}")
            data["expiry_notified_for"] = expiry_key
        data["plan_invites"] = 0
        data["plan_days"] = 0
        data["expires_at"] = None
        data["server_ip"] = None
        changed = True
    if changed:
        save_db()

@expire_plans.before_loop
async def before_expire_plans():
    await bot.wait_until_ready()

# ==================== SHARED CLAIM LOGIC ====================
async def claim_plan(user, invite_count, server_ip):
    if invite_count not in PLAN_DAYS:
        return False, "Choose a plan with **2, 4, 8, 12, 16, or 24 invites**."
    server_ip = server_ip.strip()
    if not server_ip or len(server_ip) > 150:
        return False, "Enter a valid server IP/address (maximum 150 characters)."
    data = user_data(user.id)
    if plan_active(data):
        return False, f"You already have an active plan until {format_expiry(data.get('expires_at'))}."
    available = int(data.get("invites", 0))
    if available < invite_count:
        return False, f"You need **{invite_count} invites**, but only **{available}** are available."
    days = PLAN_DAYS[invite_count]
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    data["invites"] = available - invite_count
    data["claimed_invites"] = int(data.get("claimed_invites", 0)) + invite_count
    data["plan_invites"] = invite_count
    data["plan_days"] = days
    data["expires_at"] = expiry.isoformat()
    data["expiry_notified_for"] = None
    data["server_ip"] = server_ip
    save_db()
    return True, f"Plan activated for **{days} days**!\nServer: `{server_ip}`\nExpires: {format_expiry(data['expires_at'])}\nRemaining invites: **{data['invites']}**."

# ==================== SLASH COMMANDS ====================
@bot.tree.command(name="help", description="Show VorstyAFK commands and plans")
@app_commands.guild_only()
async def slash_help(interaction: discord.Interaction):
    if not allowed_guild(interaction.guild):
        return await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
    embed = discord.Embed(title="VorstyAFK • Help & Plans", description="Invite rewards and plan management", color=discord.Color.blurple())
    embed.add_field(name="Commands", value="`/claim invite ip` — claim a plan\n`/checkinvits` — check invites and plan\n`/help` — show this help", inline=False)
    embed.add_field(name="Available plans", value=plan_list_text(), inline=False)
    embed.add_field(name="Upgrade", value=f"`$upgrade` submits an upgrade request and costs **{UPGRADE_COST} available invites**. If you have fewer, the request is rejected and no invites are deducted.", inline=False)
    embed.set_footer(text="One active plan per user. Keep your invites available before requesting an upgrade.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="claim", description="Claim a plan using your invites")
@app_commands.guild_only()
@app_commands.describe(invite="Choose the plan invite cost", ip="Your Minecraft server IP/address")
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
        return await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
    ok, message = await claim_plan(interaction.user, invite.value, ip)
    await interaction.response.send_message(("✅ " if ok else "❌ ") + message, ephemeral=True)
    if ok:
        data = user_data(interaction.user.id)
        await send_admin(f"🎟️ **Plan claimed**\nUser: {interaction.user.mention} (`{interaction.user.id}`)\nPlan: **{invite.value} invites / {PLAN_DAYS[invite.value]} days**\nServer: `{ip.strip()}`\nExpires: {format_expiry(data.get('expires_at'))}")

@bot.tree.command(name="checkinvits", description="Check your invites and active plan")
@app_commands.guild_only()
async def slash_checkinvits(interaction: discord.Interaction):
    if not allowed_guild(interaction.guild):
        return await interaction.response.send_message("This bot is not configured for this server.", ephemeral=True)
    data = user_data(interaction.user.id)
    active = plan_active(data)
    embed = discord.Embed(title="Your VorstyAFK Stats", color=discord.Color.green())
    embed.add_field(name="Available invites", value=str(data.get("invites", 0)), inline=True)
    embed.add_field(name="Invites used on claims", value=str(data.get("claimed_invites", 0)), inline=True)
    embed.add_field(name="Active plan", value=(f"{data.get('plan_invites', 0)} invites / {data.get('plan_days', 0)} days" if active else "None"), inline=False)
    embed.add_field(name="Expires", value=(format_expiry(data.get("expires_at")) if active else "No active plan"), inline=False)
    embed.add_field(name="Server", value=(f"`{data.get('server_ip')}`" if active and data.get("server_ip") else "Not set"), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==================== PREFIX COMMANDS ====================
@bot.command(name="help")
async def prefix_help(ctx):
    embed = discord.Embed(title="VorstyAFK • Help & Plans", color=discord.Color.blurple())
    embed.description = "**Slash:** `/help`, `/claim`, `/checkinvits`\n**User:** `$claim <count> ip: <server>`, `$check invite`, `$checkinvites`, `$upgrade [details]`\n**Admin:** `$setinvite @user amount`, `$userstatus @user`, `$dm @user message`"
    embed.add_field(name="Plans", value=plan_list_text(), inline=False)
    embed.add_field(name="Upgrade rule", value=f"Upgrade request costs **{UPGRADE_COST} invites**. No request is sent and no invites are deducted if balance is below {UPGRADE_COST}.", inline=False)
    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="claim")
async def prefix_claim(ctx, invite_count: int, *, ip: str):
    if not allowed_guild(ctx.guild):
        return await ctx.reply("This bot is not configured for this server.", mention_author=False)
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
    active = plan_active(data)
    await ctx.reply(f"📨 Available invites: **{data.get('invites', 0)}**\n🎟️ Claimed invites: **{data.get('claimed_invites', 0)}**\n📦 Active plan: **{data.get('plan_days', 0) if active else 0} days**\n⏳ Expires: {format_expiry(data.get('expires_at')) if active else 'No active plan'}", mention_author=False)

@bot.command(name="checkinvites", aliases=["checkinvits"])
async def prefix_checkinvites(ctx):
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
    if not allowed_guild(ctx.guild):
        return await ctx.reply("This bot is not configured for this server.", mention_author=False)
    data = user_data(ctx.author.id)
    available = int(data.get("invites", 0))
    if available < UPGRADE_COST:
        return await ctx.reply(f"❌ Upgrade request ke liye **{UPGRADE_COST} invites** chahiye. Tumhare paas **{available}** hain. Invite balance kam hai, request send nahi hui aur koi invite deduct nahi hua.", mention_author=False)
    # Deduct only after validating the required balance.
    data["invites"] = available - UPGRADE_COST
    request = {
        "user_id": ctx.author.id,
        "details": details[:500],
        "cost": UPGRADE_COST,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    db["upgrade_requests"].append(request)
    save_db()
    await ctx.reply(f"✅ Upgrade request admin ko bhej di. **{UPGRADE_COST} invites** use hue; remaining invites: **{data['invites']}**. Admin approval pending hai.", mention_author=False)
    await send_to_channel(UPGRADE_ALERT_CHANNEL_ID, f"⬆️ **Upgrade request**\nUser: {ctx.author.mention} (`{ctx.author.id}`)\nCurrent plan: {data.get('plan_days', 0)} days\nInvite cost: **{UPGRADE_COST}**\nRemaining invites: **{data['invites']}**\nDetails: {details[:500] or 'No details provided'}\nStatus: **Pending admin review**")

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
    try:
        await ctx.reply("Something went wrong. Check the Render logs.", mention_author=False)
    except discord.HTTPException:
        pass

async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Add it in Render → Environment.")
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
