import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import os
import logging
import random
import aiohttp
import asyncpraw
from flask import Flask
from threading import Thread
from pymongo import MongoClient
from datetime import datetime
import warnings

# ===================== AUDIOOP PATCH (for Python 3.13) =====================
try:
    import audioop
except ImportError:
    import sys
    import types
    audioop = types.ModuleType("audioop")
    sys.modules["audioop"] = audioop
    audioop.error = Exception
# ==========================================================================

# =========================== CONFIGS ===========================
TOKEN = os.getenv("DISCORD_TOKEN")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "discord:nsfw-bot:v1.0 (by u/Efficient-Life-554)")
MONGO_URI = os.getenv("MONGO_URI")

LOG_CHANNEL_ID = 1391882689069580360
BOT_OWNER_ID = 887243211645546517

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

reddit = asyncpraw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    user_agent=REDDIT_USER_AGENT
)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["reddit_bot"]
mapping_col = db["subreddit_channel_mapping"]
config_col = db["global_config"]

# ========== INITIAL CONFIG ==========
GLOBAL_POST_INTERVAL = config_col.find_one({"_id": "global"}) or {"_id": "global", "interval": 30}
config_col.update_one({"_id": "global"}, {"$set": GLOBAL_POST_INTERVAL}, upsert=True)

# ======================= LOGGING SETUP ========================
logging.basicConfig(level=logging.INFO)

# =============== FLASK SERVER FOR UPTIME ======================
app = Flask("")

@app.route("/")
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

Thread(target=run).start()

# ======================== HELPERS ==============================

async def fetch_post(subreddit_name):
    try:
        subreddit = await reddit.subreddit(subreddit_name, fetch=True)
        posts = [post async for post in subreddit.hot(limit=50) if not post.stickied and post.over_18]
        return random.choice(posts) if posts else None
    except Exception:
        return None

async def send_to_channel(channel_id, post):
    channel = bot.get_channel(channel_id)
    if not channel:
        mapping_col.delete_many({"channel_id": channel_id})
        return
    embed = discord.Embed(title=post.title, url=post.url, color=discord.Color.red())
    embed.set_image(url=post.url)
    await channel.send(embed=embed)

async def log_error(message: str):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        await channel.send(f"⚠️ {message}")

async def dm_owner(error: str):
    owner = bot.get_user(BOT_OWNER_ID)
    if owner:
        try:
            await owner.send(f"🚨 Bot Error:\n```{error}```")
        except:
            pass

def is_admin(interaction: discord.Interaction):
    return interaction.user.guild_permissions.administrator

# ======================= COMMANDS ==============================

@tree.command(name="addsub", description="Link a subreddit to this channel.")
@app_commands.describe(subreddit="Subreddit name")
async def addsub(interaction: discord.Interaction, subreddit: str):
    await interaction.response.defer(ephemeral=True)
    mapping_col.update_one(
        {"channel_id": interaction.channel_id},
        {"$set": {"subreddit": subreddit.lower(), "limit": 1, "interval": 60}},
        upsert=True
    )
    await interaction.followup.send(f"✅ Linked this channel to r/{subreddit}")

@tree.command(name="removesub", description="Unlink subreddit from this channel.")
async def removesub(interaction: discord.Interaction):
    mapping_col.delete_one({"channel_id": interaction.channel_id})
    await interaction.response.send_message("❌ Subreddit unlinked from this channel.", ephemeral=True)

@tree.command(name="listsubs", description="List all channel-subreddit links.")
async def listsubs(interaction: discord.Interaction):
    data = mapping_col.find()
    msg = "\n".join([f"<#{d['channel_id']}> ➜ r/{d['subreddit']}" for d in data])
    await interaction.response.send_message(f"📜 Linked Channels:\n{msg or 'None'}", ephemeral=True)

@tree.command(name="setlimit", description="Set max posts per send.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(limit="Number of posts per send (1-10)")
async def setlimit(interaction: discord.Interaction, limit: int):
    if not (1 <= limit <= 10):
        await interaction.response.send_message("⚠️ Limit must be between 1 and 10.", ephemeral=True)
        return
    mapping_col.update_one({"channel_id": interaction.channel_id}, {"$set": {"limit": limit}})
    await interaction.response.send_message(f"✅ Set limit to {limit}", ephemeral=True)

@tree.command(name="setinterval", description="Set interval between posts in minutes.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(minutes="Interval in minutes (minimum 1)")
async def setinterval(interaction: discord.Interaction, minutes: int):
    if minutes < 1:
        await interaction.response.send_message("⚠️ Interval must be at least 1 minute.", ephemeral=True)
        return
    mapping_col.update_one({"channel_id": interaction.channel_id}, {"$set": {"interval": minutes}})
    await interaction.response.send_message(f"✅ Set interval to {minutes} min", ephemeral=True)

@tree.command(name="setglobalinterval", description="Set global post interval (for all channels).")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(minutes="Global interval (minutes)")
async def setglobalinterval(interaction: discord.Interaction, minutes: int):
    if minutes < 1:
        await interaction.response.send_message("⚠️ Interval must be at least 1 min.", ephemeral=True)
        return
    config_col.update_one({"_id": "global"}, {"$set": {"interval": minutes}}, upsert=True)
    await interaction.response.send_message(f"✅ Global post interval set to {minutes} min", ephemeral=True)
    global GLOBAL_POST_INTERVAL
    GLOBAL_POST_INTERVAL["interval"] = minutes
    global_poster.restart()

@tree.command(name="send", description="Send a post from this channel's subreddit.")
@app_commands.checks.cooldown(1, 10.0)
async def send(interaction: discord.Interaction):
    await interaction.response.defer()
    mapping = mapping_col.find_one({"channel_id": interaction.channel_id})
    if not mapping:
        await interaction.followup.send("⚠️ No subreddit linked to this channel.")
        return
    post = await fetch_post(mapping["subreddit"])
    if not post:
        await interaction.followup.send("⚠️ Couldn't fetch post.")
        return
    embed = discord.Embed(title=post.title, url=post.url, color=discord.Color.red())
    embed.set_image(url=post.url)
    await interaction.followup.send(embed=embed)

@tree.command(name="forcesend", description="Send posts from all linked subreddits.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(count="Max posts per channel (default 1)")
async def forcesend(interaction: discord.Interaction, count: int = 1):
    await interaction.response.defer(ephemeral=True)
    data = mapping_col.find()
    for mapping in data:
        for _ in range(min(count, mapping.get("limit", 1))):
            post = await fetch_post(mapping["subreddit"])
            if post:
                await send_to_channel(mapping["channel_id"], post)
    await interaction.followup.send("✅ Forced post complete.")

# ================ GLOBAL POSTING TASK =================

@tasks.loop(minutes=GLOBAL_POST_INTERVAL["interval"])
async def global_poster():
    data = mapping_col.find()
    for mapping in data:
        interval = mapping.get("interval", GLOBAL_POST_INTERVAL["interval"])
        last_post_time = mapping.get("last_post_time")
        now = datetime.utcnow()
        if not last_post_time or (now - last_post_time).total_seconds() >= interval * 60:
            for _ in range(mapping.get("limit", 1)):
                post = await fetch_post(mapping["subreddit"])
                if post:
                    await send_to_channel(mapping["channel_id"], post)
            mapping_col.update_one({"channel_id": mapping["channel_id"]}, {"$set": {"last_post_time": now}})

@global_poster.before_loop
async def before_global_poster():
    await bot.wait_until_ready()

# =================== EVENTS ===================

@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {bot.user}")
    global_poster.start()

@bot.event
async def on_guild_channel_delete(channel):
    mapping_col.delete_one({"channel_id": channel.id})

@bot.event
async def on_command_error(ctx, error):
    await log_error(str(error))
    await dm_owner(str(error))

# ================ RUN BOT ===================
bot.run(TOKEN)
