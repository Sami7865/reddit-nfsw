import sys

# Monkey-patch to fake audioop for discord.py on Python 3.13+
if sys.version_info >= (3, 13):
    import types
    sys.modules['audioop'] = types.ModuleType('audioop')

import os
import asyncio
import logging
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta, UTC  # Add UTC import

import discord
from discord import app_commands
from discord.ext import commands, tasks

from pymongo import MongoClient
import asyncpraw

# ─── Flask Keepalive Server ─────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

Thread(target=run_flask).start()

# ─── Environment Variables ──────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME = os.getenv("REDDIT_USERNAME")
REDDIT_PASSWORD = os.getenv("REDDIT_PASSWORD")
MONGO_URI = os.getenv("MONGO_URI")

# Check for required environment variables
missing_vars = []
if not TOKEN:
    missing_vars.append("DISCORD_TOKEN")
if not REDDIT_CLIENT_ID:
    missing_vars.append("REDDIT_CLIENT_ID")
if not REDDIT_CLIENT_SECRET:
    missing_vars.append("REDDIT_CLIENT_SECRET")
if not REDDIT_USERNAME:
    missing_vars.append("REDDIT_USERNAME")
if not REDDIT_PASSWORD:
    missing_vars.append("REDDIT_PASSWORD")
if not MONGO_URI:
    missing_vars.append("MONGO_URI")
if missing_vars:
    print(f"[FATAL] Missing required environment variables: {', '.join(missing_vars)}")
    print("Please set these in your Render environment settings.")
    import sys; sys.exit(1)

BOT_OWNER_ID = 887243211645546517
LOGGING_CHANNEL_ID = 1391882689069580360
GUILD_ID = 1369650511208513636

# ─── Discord Bot Setup ──────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds = intents.messages = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── MongoDB Setup ──────────────────────────────────────────────────────────────
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["reddit_bot"]
config_col = db["configs"]

# ─── Reddit Client ──────────────────────────────────────────────────────────────
reddit = asyncpraw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    username=REDDIT_USERNAME,
    password=REDDIT_PASSWORD,
    user_agent="Discord NSFW Bot by u/Efficient-Life-554"
)

# ─── Globals ────────────────────────────────────────────────────────────────────
GLOBAL_POST_INTERVAL = 30  # default to 30 minutes
LAST_SENT = {}

# ─── Utility Functions ──────────────────────────────────────────────────────────
async def send_error_dm(user_id: int, message: str):
    user = await bot.fetch_user(user_id)
    if user:
        await user.send(f"⚠️ Bot Error:\n```\n{message}\n```")

def is_admin_or_mod(interaction: discord.Interaction):
    return interaction.user.guild_permissions.manage_guild

def get_config(channel_id: int):
    return config_col.find_one({"channel_id": channel_id}) or {}

async def fetch_post(subreddit: str):
    try:
        sub = await reddit.subreddit(subreddit, fetch=True)
        async for post in sub.hot(limit=25):
            if not post.over_18 or post.stickied:
                continue
            if post.url.endswith((".jpg", ".png", ".gif", ".jpeg", ".webm", ".mp4")) or "v.redd.it" in post.url:
                return post
        return None
    except Exception as e:
        return None

async def build_embed(post):
    embed = discord.Embed(
        title=post.title[:256],
        url=f"https://reddit.com{post.permalink}",
        description=f"👍 {post.score} | 💬 {post.num_comments}",
        timestamp=datetime.utcfromtimestamp(post.created_utc),
        color=discord.Color.red()
    )
    if "v.redd.it" in post.url and post.media:
        video_url = post.media.get("reddit_video", {}).get("fallback_url")
        if video_url:
            embed.add_field(name="Video", value=video_url, inline=False)
    elif post.url:
        embed.set_image(url=post.url)
    embed.set_footer(text=f"Posted by u/{post.author}")
    return embed

# ─── Commands ───────────────────────────────────────────────────────────────────

@tree.command(
    name="addsub",
    description="Link a subreddit to this channel."
)
@app_commands.describe(
    name="Subreddit name (without r/)"
)
async def addsub(interaction: discord.Interaction, name: str):
    if not is_admin_or_mod(interaction):
        return await interaction.response.send_message("You must be an admin/mod to use this.", ephemeral=True)
    
    try:
        # Check subreddit first before deferring
        sub = await reddit.subreddit(name, fetch=True)
        if not sub.over18:
            return await interaction.response.send_message("❌ Subreddit is not NSFW.", ephemeral=True)
            
        await interaction.response.defer(thinking=True)
        
        config_col.update_one(
            {"channel_id": interaction.channel_id},
            {"$addToSet": {"subs": name.lower()}, "$setOnInsert": {
                "interval": GLOBAL_POST_INTERVAL,
                "limit": 25
            }},
            upsert=True
        )
        await interaction.followup.send(f"✅ Added r/{name} to this channel.")
    except asyncpraw.exceptions.InvalidURL:
        await interaction.response.send_message(f"❌ Invalid subreddit name: r/{name}", ephemeral=True)
    except Exception as e:
        # If we haven't responded yet, send an immediate response
        try:
            await interaction.response.send_message(f"❌ Failed to add r/{name}: {e}", ephemeral=True)
        except discord.errors.InteractionResponded:
            await interaction.followup.send(f"❌ Failed to add r/{name}: {e}", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

@tree.command(
    name="removesub",
    description="Unlink a subreddit from this channel."
)
@app_commands.describe(
    name="Subreddit name (without r/)"
)
async def removesub(interaction: discord.Interaction, name: str):
    if not is_admin_or_mod(interaction):
        return await interaction.response.send_message("You must be an admin/mod to use this.", ephemeral=True)
    try:
        config_col.update_one({"channel_id": interaction.channel_id}, {"$pull": {"subs": name.lower()}})
        await interaction.response.send_message(f"🗑️ Removed r/{name} from this channel.")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error removing subreddit: {e}", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

@tree.command(
    name="listsubs",
    description="List all subreddits linked to this channel."
)
async def listsubs(interaction: discord.Interaction):
    try:
        cfg = get_config(interaction.channel_id)
        subs = cfg.get("subs", [])
        if not subs:
            return await interaction.response.send_message("❌ No subreddits linked.")
        await interaction.response.send_message("📜 Subreddits:\n" + "\n".join(f"- r/{s}" for s in subs))
    except Exception as e:
        await interaction.response.send_message("❌ Error listing subreddits.", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

@tree.command(
    name="setinterval",
    description="Set post interval (minutes) for this channel."
)
@app_commands.describe(
    minutes="Minutes between posts (1-1440)"
)
@app_commands.choices(
    minutes=[
        app_commands.Choice(name=f"{i} minutes", value=i)
        for i in [1, 5, 10, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1440]
    ]
)
async def setinterval(interaction: discord.Interaction, minutes: int):
    if not is_admin_or_mod(interaction):
        return await interaction.response.send_message("Admin only.", ephemeral=True)
    try:
        if not 1 <= minutes <= 1440:
            return await interaction.response.send_message("❌ Interval must be between 1 and 1440 minutes.", ephemeral=True)
        config_col.update_one({"channel_id": interaction.channel_id}, {"$set": {"interval": minutes}})
        await interaction.response.send_message(f"⏱️ Interval set to {minutes} min.")
    except Exception as e:
        await interaction.response.send_message("❌ Error setting interval.", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

@tree.command(
    name="setglobalinterval",
    description="Set global post interval for all channels."
)
@app_commands.describe(
    minutes="Global minutes between posts (1-1440)"
)
@app_commands.choices(
    minutes=[
        app_commands.Choice(name=f"{i} minutes", value=i)
        for i in [1, 5, 10, 15, 30, 60, 120, 180, 240, 360, 480, 720, 1440]
    ]
)
async def setglobalinterval(interaction: discord.Interaction, minutes: int):
    if not is_admin_or_mod(interaction):
        return await interaction.response.send_message("Admin only.", ephemeral=True)
    try:
        if not 1 <= minutes <= 1440:
            return await interaction.response.send_message("❌ Interval must be between 1 and 1440 minutes.", ephemeral=True)
        global GLOBAL_POST_INTERVAL
        GLOBAL_POST_INTERVAL = minutes
        await interaction.response.send_message(f"🌐 Global interval set to {minutes} min.")
    except Exception as e:
        await interaction.response.send_message("❌ Error setting global interval.", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

@tree.command(
    name="send",
    description="Manually send a post to this channel."
)
async def send(interaction: discord.Interaction):
    try:
        cfg = get_config(interaction.channel_id)
        subs = cfg.get("subs", [])
        if not subs:
            return await interaction.response.send_message("❌ No subreddits linked.")
        
        await interaction.response.defer(thinking=True)
        
        sub = subs[datetime.now(UTC).second % len(subs)]
        post = await fetch_post(sub)
        if not post:
            return await interaction.followup.send("⚠️ No valid post found.")
        embed = await build_embed(post)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        try:
            await interaction.response.send_message("❌ Error sending post.", ephemeral=True)
        except discord.errors.InteractionResponded:
            await interaction.followup.send("❌ Error sending post.", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

@tree.command(
    name="forcesend",
    description="Force send posts to all configured channels."
)
@app_commands.describe(
    count="How many posts per channel (1-5)"
)
@app_commands.choices(
    count=[
        app_commands.Choice(name=str(i), value=i)
        for i in range(1, 6)
    ]
)
async def forcesend(interaction: discord.Interaction, count: int = 1):
    if not is_admin_or_mod(interaction):
        return await interaction.response.send_message("Admin only.", ephemeral=True)
    try:
        if not 1 <= count <= 5:
            return await interaction.response.send_message("❌ Count must be between 1 and 5.", ephemeral=True)
            
        await interaction.response.defer(thinking=True)
        
        success_count = 0
        fail_count = 0
        
        for cfg in config_col.find():
            channel = bot.get_channel(cfg["channel_id"])
            if not channel:
                config_col.delete_one({"channel_id": cfg["channel_id"]})
                continue
                
            if "subs" not in cfg or not cfg["subs"]:
                continue
                
            for _ in range(count):
                try:
                    sub = cfg["subs"][datetime.now(UTC).second % len(cfg["subs"])]
                    post = await fetch_post(sub)
                    if post:
                        embed = await build_embed(post)
                        await channel.send(embed=embed)
                        success_count += 1
                except Exception:
                    fail_count += 1
                    continue
        
        await interaction.followup.send(f"✅ Force send complete!\nSuccess: {success_count}\nFailed: {fail_count}")
    except Exception as e:
        try:
            await interaction.response.send_message("❌ Error during force send.", ephemeral=True)
        except discord.errors.InteractionResponded:
            await interaction.followup.send("❌ Error during force send.", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

# ─── Auto Poster Task ───────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def auto_post_loop():
    for cfg in config_col.find():
        channel_id = cfg["channel_id"]
        interval = cfg.get("interval", GLOBAL_POST_INTERVAL)
        last_time = LAST_SENT.get(channel_id, datetime.min.replace(tzinfo=UTC))
        if datetime.now(UTC) - last_time < timedelta(minutes=interval):
            continue
        channel = bot.get_channel(channel_id)
        if not channel:
            config_col.delete_one({"channel_id": channel_id})
            continue
        if not cfg.get("subs"):
            continue
        sub = cfg["subs"][datetime.now(UTC).second % len(cfg["subs"])]
        post = await fetch_post(sub)
        if post:
            try:
                embed = await build_embed(post)
                await channel.send(embed=embed)
                LAST_SENT[channel_id] = datetime.now(UTC)
            except Exception:
                continue

# ─── Bot Events ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    try:
        # Sync commands globally first
        await tree.sync()
        # Then sync to specific guild
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        auto_post_loop.start()
        logging_channel = bot.get_channel(LOGGING_CHANNEL_ID)
        if logging_channel:
            await logging_channel.send(f"✅ Bot restarted at {datetime.now(UTC)}")
    except Exception as e:
        print(f"Error during startup: {e}")
        if logging_channel:
            await logging_channel.send(f"⚠️ Error during startup: {e}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Cooldown: Try again in {round(error.retry_after)}s", delete_after=5)
    else:
        await send_error_dm(BOT_OWNER_ID, str(error))

# ─── Run Bot ────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
