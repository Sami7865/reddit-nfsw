import os
import random
import time
import discord
import threading
from flask import Flask
from discord.ext import tasks
from discord import app_commands
from pymongo import MongoClient
import praw

# Audioop patch for Python 3.13+
import sys
if sys.version_info >= (3, 13):
    import types
    sys.modules['audioop'] = types.SimpleNamespace()

# Constants
SEND_COOLDOWN_SECONDS = 10
send_cooldowns = {}

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Flask keep-alive
app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is alive!"
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8080)).start()

# Reddit setup
reddit = praw.Reddit(
    client_id=os.getenv("CLIENT_ID"),
    client_secret=os.getenv("CLIENT_SECRET"),
    user_agent=os.getenv("USER_AGENT")
)

# MongoDB setup
mongo = MongoClient(os.getenv("MONGO_URI"))
db = mongo["nsfw_bot"]
subs_col = db["subreddit_channels"]
posts_col = db["sent_posts"]
intervals_col = db["guild_intervals"]

# Helper: fetch Reddit post
def fetch_post(subreddit_name):
    try:
        subreddit = reddit.subreddit(subreddit_name)
        posts = list(subreddit.hot(limit=50))
        random.shuffle(posts)
        for post in posts:
            if not post.over_18 or posts_col.find_one({"post_id": post.id}):
                continue
            if post.url.endswith((".jpg", ".png", ".gif", ".mp4", ".webm")):
                media_url = post.url
            elif post.is_video and post.media and 'reddit_video' in post.media:
                media_url = post.media['reddit_video']['fallback_url']
            else:
                continue
            posts_col.insert_one({"post_id": post.id})
            return {
                "title": post.title,
                "url": media_url,
                "permalink": f"https://reddit.com{post.permalink}",
                "score": post.score,
                "subreddit": subreddit_name
            }
    except:
        return None

# Admin check
def is_admin(interaction):
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild

# Slash: Add subreddit
@tree.command(name="addsub", description="Link a subreddit to this channel")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(subreddit="Subreddit name")
async def addsub(interaction: discord.Interaction, subreddit: str):
    query = {"subreddit": subreddit, "channel_id": interaction.channel_id, "guild_id": interaction.guild_id}
    if subs_col.find_one(query):
        await interaction.response.send_message("⚠️ Already linked.", ephemeral=True)
    else:
        subs_col.insert_one(query)
        await interaction.response.send_message(f"✅ Linked r/{subreddit} to this channel.", ephemeral=True)

# Slash: Remove subreddit
@tree.command(name="removesub", description="Unlink a subreddit")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(subreddit="Subreddit name")
async def removesub(interaction: discord.Interaction, subreddit: str):
    result = subs_col.delete_one({"subreddit": subreddit, "channel_id": interaction.channel_id, "guild_id": interaction.guild_id})
    if result.deleted_count:
        await interaction.response.send_message(f"✅ Unlinked r/{subreddit}.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Not found.", ephemeral=True)

# Slash: Set limit
@tree.command(name="setlimit", description="Set post limit per cycle")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(subreddit="Subreddit name", count="Posts per cycle")
async def setlimit(interaction: discord.Interaction, subreddit: str, count: int):
    if count < 1 or count > 10:
        await interaction.response.send_message("⚠️ Limit must be 1–10.", ephemeral=True)
        return
    query = {"subreddit": subreddit, "channel_id": interaction.channel_id, "guild_id": interaction.guild_id}
    if not subs_col.find_one(query):
        await interaction.response.send_message("❌ Subreddit not linked.", ephemeral=True)
        return
    subs_col.update_one(query, {"$set": {"limit": count}})
    await interaction.response.send_message(f"✅ Updated limit for r/{subreddit} to {count}.", ephemeral=True)

# Slash: Set interval
@tree.command(name="setinterval", description="Set auto-post interval")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(minutes="Interval in minutes")
async def setinterval(interaction: discord.Interaction, minutes: int):
    if minutes < 1 or minutes > 1440:
        await interaction.response.send_message("⚠️ Interval must be 1–1440.", ephemeral=True)
        return
    intervals_col.update_one({"guild_id": interaction.guild_id}, {"$set": {"interval": minutes}}, upsert=True)
    await interaction.response.send_message(f"✅ Interval set to {minutes} min.", ephemeral=True)

# Slash: Send post (respect channel mapping)
@tree.command(name="send", description="Send post from subreddit mapped to this channel")
async def send(interaction: discord.Interaction):
    user_id = interaction.user.id
    if not is_admin(interaction):
        last = send_cooldowns.get(user_id, 0)
        now = time.time()
        if now - last < SEND_COOLDOWN_SECONDS:
            wait = SEND_COOLDOWN_SECONDS - (now - last)
            await interaction.response.send_message(f"⏳ Cooldown: {int(wait)}s", ephemeral=True)
            return
        send_cooldowns[user_id] = now

    mapping = subs_col.find_one({"channel_id": interaction.channel_id, "guild_id": interaction.guild_id})
    if not mapping:
        await interaction.response.send_message("❌ No subreddit mapped to this channel.", ephemeral=True)
        return
    await interaction.response.defer()
    post = fetch_post(mapping['subreddit'])
    if post:
        embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
        embed.description = f"[🔗 Media Link]({post['url']})"
        embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send("❌ No post found.")

# Slash: Force post
@tree.command(name="forcepost", description="Force post from linked subreddit")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(subreddit="Subreddit name")
async def forcepost(interaction: discord.Interaction, subreddit: str):
    entry = subs_col.find_one({"subreddit": subreddit, "guild_id": interaction.guild_id})
    if not entry:
        await interaction.response.send_message("❌ Not linked.", ephemeral=True)
        return
    channel = bot.get_channel(entry["channel_id"])
    post = fetch_post(subreddit)
    if post and channel:
        embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
        embed.description = f"[🔗 Media Link]({post['url']})"
        embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
        await channel.send(embed=embed)
        await interaction.response.send_message("✅ Post sent.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ No content or channel.", ephemeral=True)

# Slash: Force send all
@tree.command(name="forcesend", description="Force send from all linked subreddits")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(count="Posts per subreddit (default 1)")
async def forcesend(interaction: discord.Interaction, count: int = 1):
    await interaction.response.defer(thinking=True, ephemeral=True)
    if count < 1 or count > 10:
        await interaction.followup.send("⚠️ Count must be 1–10.")
        return
    total = 0
    for mapping in subs_col.find({"guild_id": interaction.guild_id}):
        channel = bot.get_channel(mapping["channel_id"])
        if not channel:
            continue
        for _ in range(count):
            post = fetch_post(mapping["subreddit"])
            if post:
                embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
                embed.description = f"[🔗 Media Link]({post['url']})"
                embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
                try:
                    await channel.send(embed=embed)
                    total += 1
                except:
                    break
    await interaction.followup.send(f"✅ Sent {total} post(s).")

# Slash: List linked subreddits
@tree.command(name="listsubs", description="Show linked subreddits")
async def listsubs(interaction: discord.Interaction):
    mappings = subs_col.find({"guild_id": interaction.guild_id})
    embed = discord.Embed(title="📄 Linked Subreddits", color=discord.Color.blurple())
    found = False
    for entry in mappings:
        found = True
        channel = bot.get_channel(entry["channel_id"])
        ch_mention = f"<#{entry['channel_id']}>" if channel else "unknown"
        limit = entry.get("limit", 1)
        embed.add_field(name=f"r/{entry['subreddit']}", value=f"{ch_mention} • Limit: {limit}", inline=False)
    if found:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("📭 No links yet.", ephemeral=True)

# Auto-poster
@tasks.loop(minutes=5)
async def auto_post():
    grouped = {}
    for entry in subs_col.find():
        key = (entry["guild_id"], entry["channel_id"])
        grouped.setdefault(key, []).append(entry)
    for (guild_id, channel_id), mappings in grouped.items():
        interval_doc = intervals_col.find_one({"guild_id": guild_id})
        interval = interval_doc["interval"] if interval_doc else 10
        if auto_post.current_loop % (interval // 5) != 0:
            continue
        channel = bot.get_channel(channel_id)
        if not channel or not channel.is_nsfw():
            continue
        for mapping in mappings:
            post_limit = mapping.get("limit", 1)
            for _ in range(post_limit):
                post = fetch_post(mapping["subreddit"])
                if not post:
                    break
                embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
                embed.description = f"[🔗 Media Link]({post['url']})"
                embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
                try:
                    await channel.send(embed=embed)
                except:
                    break

# Ready event
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    dev_guild = discord.Object(id=1369650511208513636)
    try:
        await tree.sync()
        await tree.sync(guild=dev_guild)
        print("✅ Slash commands synced.")
    except Exception as e:
        print(f"❌ Sync error: {e}")
    if not auto_post.is_running():
        auto_post.start()

bot.run(os.getenv("DISCORD_TOKEN"))
