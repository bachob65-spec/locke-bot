import asyncio
import os
import json
import re
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque
from datetime import timedelta

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")


def _run_web_server():
    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive")

        def log_message(self, *args):
            pass

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def keep_alive():
    threading.Thread(target=_run_web_server, daemon=True).start()


WARNINGS_FILE = "warnings.json"
WARN_LIMIT = 3
AUTO_MUTE_MINUTES = 10

SPAM_MESSAGE_LIMIT = 5
SPAM_INTERVAL_SECONDS = 5
SPAM_MUTE_SECONDS = 30

BOT_VERSION = "1.0"

PATCH_NOTES_TAG_URL = "https://www.leagueoflegends.com/en-us/news/tags/patch-notes/"
PATCH_LINK_PATTERN = re.compile(r'href="(/en-us/news/game-updates/league-of-legends-patch-[\d-]+-notes)"')

DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CHAMPION_LIST_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"

OPGG_BUILD_URL = "https://op.gg/lol/champions/{slug}/build"
OPGG_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OPGG_BUILD_SECTIONS = {"Starter items", "Boots", "Core builds", "Fourth Item", "Fifth Item", "Sixth Item"}
OPGG_BUILD_TABLE_JS = """
() => {
  const tables = document.querySelectorAll('table');
  const result = [];
  for (const table of tables) {
    const header = table.querySelector('thead th');
    const label = header ? header.textContent.trim() : '';
    const firstRow = table.querySelector('tbody tr');
    if (!firstRow) continue;
    const items = Array.from(firstRow.querySelectorAll('img')).map(img => img.alt).filter(Boolean);
    const cells = firstRow.querySelectorAll('td');
    const winRate = cells.length >= 3 ? cells[cells.length - 1].textContent.trim() : '';
    result.push({ label, items, winRate });
  }
  return result;
}
"""

BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

_browser_lock = asyncio.Lock()
_browser = None


def ensure_browser_installed():
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Playwright browser install failed: {e!r}")


async def get_browser():
    global _browser
    async with _browser_lock:
        if _browser is None:
            playwright = await async_playwright().start()
            _browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
    return _browser


async def _block_heavy_resources(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


def normalize_champ_name(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def load_warnings():
    if os.path.exists(WARNINGS_FILE):
        with open(WARNINGS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_warnings(data):
    with open(WARNINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


warnings_data = load_warnings()


def get_warn_count(guild_id, user_id):
    return warnings_data.get(str(guild_id), {}).get(str(user_id), 0)


def set_warn_count(guild_id, user_id, count):
    guild_key, user_key = str(guild_id), str(user_id)
    warnings_data.setdefault(guild_key, {})[user_key] = count
    save_warnings(warnings_data)


MOD_LOG_FILE = "mod_log_channels.json"


def load_mod_log_channels():
    if os.path.exists(MOD_LOG_FILE):
        with open(MOD_LOG_FILE, "r") as f:
            return json.load(f)
    return {}


def save_mod_log_channels(data):
    with open(MOD_LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


mod_log_channels = load_mod_log_channels()


def set_mod_log_channel(guild_id, channel_id):
    mod_log_channels[str(guild_id)] = channel_id
    save_mod_log_channels(mod_log_channels)


async def log_mod_action(guild, title, description, color=discord.Color.blurple()):
    channel_id = mod_log_channels.get(str(guild.id))
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


message_times = defaultdict(deque)
spam_last_action = {}


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} — Locke's nails are sharpened and ready ⚰️")


@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        await bot.process_commands(message)
        return

    if message.author.guild_permissions.manage_messages:
        await bot.process_commands(message)
        return

    key = (message.guild.id, message.author.id)
    now = time.time()
    timestamps = message_times[key]
    timestamps.append(now)
    while timestamps and now - timestamps[0] > SPAM_INTERVAL_SECONDS:
        timestamps.popleft()

    if len(timestamps) > SPAM_MESSAGE_LIMIT and now - spam_last_action.get(key, 0) > SPAM_MUTE_SECONDS:
        spam_last_action[key] = now
        timestamps.clear()

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

        try:
            await message.author.timeout(timedelta(seconds=SPAM_MUTE_SECONDS), reason="Anti-spam")
            await message.channel.send(
                f"🚫 {message.author.mention} slow down! Muted for {SPAM_MUTE_SECONDS}s for spamming.",
                delete_after=5,
            )
            await log_mod_action(
                message.guild,
                "🚫 Anti-spam Mute",
                f"{message.author.mention} auto-muted for {SPAM_MUTE_SECONDS}s for spamming in {message.channel.mention}.",
                color=discord.Color.orange(),
            )
        except discord.Forbidden:
            await message.channel.send(
                f"🚫 {message.author.mention} please slow down with the messages.",
                delete_after=5,
            )
        return

    await bot.process_commands(message)


@bot.command()
async def ping(ctx):
    await ctx.send(f"Pong! 🏓 {round(bot.latency * 1000)}ms")


@bot.command()
async def hello(ctx):
    await ctx.send(f"Hello {ctx.author.mention} 👋")


COMMAND_LIST = (
    "!ping - Check bot speed\n"
    "!hello - Say hello\n"
    "!info - Bot info\n"
    "!patchnotes - Show the latest League of Legends patch\n"
    "!champion <name> (alias !champ) - Show champion info, role, and splash art\n"
    "!champbuild <name> (alias !build) - Show the highest-built item build from OP.GG\n"
    "!clear <number> - Delete messages (mod only)\n"
    "!warn @user [reason] - Warn a member (mod only)\n"
    "!warnings [@user] - Check warn count\n"
    "!clearwarns @user - Reset a member's warns (mod only)\n"
    f"!mute @user [minutes] [reason] - Timeout a member (mod only, default {AUTO_MUTE_MINUTES} min)\n"
    "!unmute @user - Remove a member's timeout (mod only)\n"
    "!kick @user [reason] - Kick a member (mod only)\n"
    "!ban @user [reason] - Ban a member (mod only)\n"
    "!unban <user_id> [reason] - Unban a user by ID (mod only)\n"
    "!setmodlog #channel - Set the moderation log channel (manage server only)\n"
    f"Anti-spam: sending more than {SPAM_MESSAGE_LIMIT} messages in {SPAM_INTERVAL_SECONDS}s auto-mutes for {SPAM_MUTE_SECONDS}s (mods exempt)"
)


@bot.command()
async def helpme(ctx):
    await ctx.send(f"**Commands:**\n{COMMAND_LIST}")


@bot.command()
async def info(ctx):
    await ctx.send(
        "⚰️ Locke Bot\n"
        f"Version: {BOT_VERSION}\n"
        "Made with Python + discord.py\n"
        "Prefix: !\n\n"
        f"**Commands:**\n{COMMAND_LIST}"
    )


@bot.command(name="setmodlog")
@commands.has_permissions(manage_guild=True)
async def setmodlog(ctx, channel: discord.TextChannel):
    set_mod_log_channel(ctx.guild.id, channel.id)
    await ctx.send(f"✅ Mod-log channel set to {channel.mention}.")


@setmodlog.error
async def setmodlog_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that ❌")
    elif isinstance(error, (commands.ChannelNotFound, commands.MissingRequiredArgument)):
        await ctx.send("Usage: !setmodlog #channel")


@bot.command()
async def patchnotes(ctx):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(PATCH_NOTES_TAG_URL) as resp:
                if resp.status != 200:
                    await ctx.send("Couldn't reach the patch notes page right now, try again later.")
                    return
                html = await resp.text()
    except aiohttp.ClientError:
        await ctx.send("Couldn't reach the patch notes page right now, try again later.")
        return

    match = PATCH_LINK_PATTERN.search(html)
    if not match:
        await ctx.send(f"Couldn't find the latest patch — check here: {PATCH_NOTES_TAG_URL}")
        return

    url = f"https://www.leagueoflegends.com{match.group(1)}/"
    await ctx.send(f"🩸 Latest patch notes:\n{url}")


@bot.command(name="champion", aliases=["champ"])
async def champion(ctx, *, name: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DDRAGON_VERSIONS_URL) as resp:
                if resp.status != 200:
                    await ctx.send("Couldn't reach Data Dragon right now, try again later.")
                    return
                version = (await resp.json())[0]

            list_url = DDRAGON_CHAMPION_LIST_URL.format(version=version)
            async with session.get(list_url) as resp:
                if resp.status != 200:
                    await ctx.send("Couldn't reach Data Dragon right now, try again later.")
                    return
                champions = (await resp.json())["data"]
    except aiohttp.ClientError:
        await ctx.send("Couldn't reach Data Dragon right now, try again later.")
        return

    target = normalize_champ_name(name)
    match = next(
        (c for c in champions.values() if normalize_champ_name(c["id"]) == target or normalize_champ_name(c["name"]) == target),
        None,
    )

    if match is None:
        await ctx.send(f"Couldn't find a champion matching `{name}`.")
        return

    icon_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{match['image']['full']}"
    splash_url = f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{match['id']}_0.jpg"

    embed = discord.Embed(
        title=f"{match['name']}, {match['title']}",
        description=match["blurb"],
        color=discord.Color.dark_red(),
    )
    embed.set_thumbnail(url=icon_url)
    embed.set_image(url=splash_url)
    embed.add_field(name="Roles", value=", ".join(match["tags"]), inline=True)
    embed.set_footer(text=f"Data Dragon {version}")

    await ctx.send(embed=embed)


@champion.error
async def champion_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Usage: !champion <name>")


@bot.command(name="champbuild", aliases=["build"])
@commands.cooldown(1, 10, commands.BucketType.user)
async def champbuild(ctx, *, name: str):
    slug = normalize_champ_name(name)
    url = OPGG_BUILD_URL.format(slug=slug)

    async with ctx.typing():
        try:
            browser = await get_browser()
            page = await browser.new_page(user_agent=OPGG_USER_AGENT, viewport={"width": 1280, "height": 900})
            await page.route("**/*", _block_heavy_resources)
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                if response is None or response.status == 404:
                    await ctx.send(f"Couldn't find an OP.GG build page for `{name}`.")
                    return
                try:
                    await page.wait_for_selector("table", timeout=10000)
                except PlaywrightTimeoutError:
                    await ctx.send(f"Couldn't find a build for `{name}` — check the champion name and try again.")
                    return
                sections = await page.evaluate(OPGG_BUILD_TABLE_JS)
            finally:
                await page.close()
        except Exception as e:
            print(f"champbuild error: {e!r}")
            await ctx.send("Couldn't reach OP.GG right now, try again later.")
            return

    lines = [
        f"**{s['label']}**: {', '.join(s['items'])} ({s['winRate']} win rate)"
        for s in sections
        if s["label"] in OPGG_BUILD_SECTIONS and s["items"]
    ]

    if not lines:
        await ctx.send(f"Couldn't find a build for `{name}` — check the champion name and try again.")
        return

    embed = discord.Embed(
        title=f"Highest-built {name.title()} build",
        description="\n".join(lines),
        url=url,
        color=discord.Color.dark_gold(),
    )
    embed.set_footer(text="Source: op.gg")
    await ctx.send(embed=embed)


@champbuild.error
async def champbuild_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Usage: !champbuild <name>")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Slow down — try again in {error.retry_after:.1f}s.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int):
    await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"Deleted {amount} messages 🧹", delete_after=2)


@clear.error
async def clear_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that ❌")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason: str = "No reason given"):
    count = get_warn_count(ctx.guild.id, member.id) + 1
    set_warn_count(ctx.guild.id, member.id, count)

    await ctx.send(f"⚠️ {member.mention} has been warned. Reason: {reason} (Warn {count}/{WARN_LIMIT})")
    await log_mod_action(
        ctx.guild,
        "⚠️ Warn",
        f"{member.mention} warned by {ctx.author.mention}\nReason: {reason}\nWarn count: {count}/{WARN_LIMIT}",
        color=discord.Color.yellow(),
    )

    if count >= WARN_LIMIT:
        try:
            await member.timeout(timedelta(minutes=AUTO_MUTE_MINUTES), reason="Reached warn limit")
            await ctx.send(f"🔇 {member.mention} reached {WARN_LIMIT} warns and was muted for {AUTO_MUTE_MINUTES} minutes.")
            await log_mod_action(
                ctx.guild,
                "🔇 Auto-mute (warn limit)",
                f"{member.mention} reached {WARN_LIMIT} warns and was muted for {AUTO_MUTE_MINUTES} minutes.",
                color=discord.Color.orange(),
            )
        except discord.Forbidden:
            await ctx.send("I don't have permission to mute that member.")


@warn.error
async def warn_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that ❌")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Couldn't find that member.")


@bot.command()
async def warnings(ctx, member: discord.Member = None):
    member = member or ctx.author
    count = get_warn_count(ctx.guild.id, member.id)
    await ctx.send(f"{member.mention} has {count} warning(s).")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def clearwarns(ctx, member: discord.Member):
    set_warn_count(ctx.guild.id, member.id, 0)
    await ctx.send(f"✅ Cleared warnings for {member.mention}.")
    await log_mod_action(ctx.guild, "✅ Warnings Cleared", f"{member.mention}'s warnings cleared by {ctx.author.mention}.")


@clearwarns.error
async def clearwarns_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that ❌")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, minutes: int = AUTO_MUTE_MINUTES, *, reason: str = "No reason given"):
    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        await ctx.send(f"🔇 {member.mention} has been muted for {minutes} minute(s). Reason: {reason}")
        await log_mod_action(
            ctx.guild,
            "🔇 Mute",
            f"{member.mention} muted by {ctx.author.mention} for {minutes} minute(s)\nReason: {reason}",
            color=discord.Color.orange(),
        )
    except discord.Forbidden:
        await ctx.send("I don't have permission to mute that member.")


@mute.error
async def mute_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that ❌")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Couldn't find that member.")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    try:
        await member.timeout(None)
        await ctx.send(f"🔊 {member.mention} has been unmuted.")
        await log_mod_action(ctx.guild, "🔊 Unmute", f"{member.mention} unmuted by {ctx.author.mention}.")
    except discord.Forbidden:
        await ctx.send("I don't have permission to unmute that member.")


@unmute.error
async def unmute_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that ❌")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Couldn't find that member.")


@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "No reason given"):
    try:
        await member.kick(reason=reason)
        await ctx.send(f"👢 {member.mention} has been kicked. Reason: {reason}")
        await log_mod_action(
            ctx.guild,
            "👢 Kick",
            f"{member.mention} kicked by {ctx.author.mention}\nReason: {reason}",
            color=discord.Color.red(),
        )
    except discord.Forbidden:
        await ctx.send("I don't have permission to kick that member.")


@kick.error
async def kick_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that ❌")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Couldn't find that member.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = "No reason given"):
    try:
        await member.ban(reason=reason)
        await ctx.send(f"🔨 {member.mention} has been banned. Reason: {reason}")
        await log_mod_action(
            ctx.guild,
            "🔨 Ban",
            f"{member.mention} banned by {ctx.author.mention}\nReason: {reason}",
            color=discord.Color.dark_red(),
        )
    except discord.Forbidden:
        await ctx.send("I don't have permission to ban that member.")


@ban.error
async def ban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that ❌")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Couldn't find that member.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int, *, reason: str = "No reason given"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=reason)
        await ctx.send(f"✅ {user.mention} has been unbanned.")
        await log_mod_action(ctx.guild, "✅ Unban", f"{user.mention} unbanned by {ctx.author.mention}.")
    except discord.NotFound:
        await ctx.send("That user isn't banned or doesn't exist.")
    except discord.Forbidden:
        await ctx.send("I don't have permission to unban that user.")


@unban.error
async def unban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to do that ❌")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Usage: !unban <user_id> [reason] — find the ID in the server's ban list.")


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set. Put it in a .env file next to this script (see .env.example).")

ensure_browser_installed()
keep_alive()
bot.run(TOKEN)