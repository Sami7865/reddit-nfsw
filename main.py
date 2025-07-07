import os
import sys
import random
import discord
import threading
import types

from discord.ext import tasks, commands
from discord import app_commands
from pymongo import MongoClient
from flask import Flask
import praw

# --- Audioop Patch for Python 3.13+ ---
if sys.version_info >= (3, 13):
    import types
    sys.modules['audioop'] = types.SimpleNamespace()

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# --- Flask Keep Alive (Render) ---
app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is alive!"
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

# --- MongoDB Setup ---
mongo = MongoClient(os.getenv("MONGO_URI"))
db = mongo["nsfw_bot"]
subs_col = db["subreddit_channels"]
posts_col = db["sent_posts"]
intervals_col = db["guild_intervals"]

# --- Reddit API Setup ---
reddit = praw.Reddit(
    client_id=os.getenv("CLIENT_ID"),
    client_secret=os.getenv("CLIENT_SECRET"),
    user_agent=os.getenv("USER_AGENT")
)

# --- Fetch a Valid NSFW Reddit Post ---
def fetch_post(subreddit_name):
    subreddit = reddit.subreddit(subreddit_name)
    posts = list(subreddit.hot(limit=50))
    random.shuffle(posts)
    for post in posts:
        if not post.over_18 or posts_col.find_one({"post_id": post.id}):
            continue

        media_url = ""

        if post.is_video and post.media and "reddit_video" in post.media:
            media_url = post.media["reddit_video"]["fallback_url"]

        elif post.url.endswith(('.jpg', '.png', '.gif', '.mp4', '.webm')):
            media_url = post.url

        elif "i.redd.it" in post.url or "i.imgur.com" in post.url:
            media_url = post.url

        if media_url:
            posts_col.insert_one({"post_id": post.id})
            return {
                "title": post.title,
                "url": media_url,
                "permalink": f"https://reddit.com{post.permalink}",
                "score": post.score,
                "subreddit": subreddit_name
            }

    return None

# --- Slash Command: Add Subreddit ---
@tree.command(name="addsub", description="Link a subreddit to this channel")
@app_commands.describe(subreddit="Subreddit name")
async def addsub(interaction: discord.Interaction, subreddit: str):
    query = {"subreddit": subreddit, "channel_id": interaction.channel_id, "guild_id": interaction.guild_id}
    if subs_col.find_one(query):
        await interaction.response.send_message("⚠️ Already linked.", ephemeral=True)
    else:
        subs_col.insert_one(query)
        await interaction.response.send_message(f"✅ Linked r/{subreddit} to this channel.", ephemeral=True)

# --- Slash Command: Remove Subreddit ---
@tree.command(name="removesub", description="Unlink subreddit from this channel")
@app_commands.describe(subreddit="Subreddit name")
async def removesub(interaction: discord.Interaction, subreddit: str):
    result = subs_col.delete_one({
        "subreddit": subreddit,
        "channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id
    })
    if result.deleted_count:
        await interaction.response.send_message(f"✅ Unlinked r/{subreddit}.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Subreddit not linked.", ephemeral=True)

# --- Slash Command: Set Post Limit (Admin Only) ---
@tree.command(name="setlimit", description="Set number of posts to send per cycle")
@app_commands.describe(subreddit="Subreddit name", count="Posts per cycle (1–10)")
async def setlimit(interaction: discord.Interaction, subreddit: str, count: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return
    if count < 1 or count > 10:
        await interaction.response.send_message("⚠️ Limit must be 1–10.", ephemeral=True)
        return
    query = {
        "subreddit": subreddit,
        "channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id
    }
    subs_col.update_one(query, {"$set": {"limit": count}})
    await interaction.response.send_message(f"✅ Set r/{subreddit} limit to {count} in this channel.", ephemeral=True)

# --- Slash Command: Set Interval (Admin Only) ---
@tree.command(name="setinterval", description="Set auto-post interval in minutes (admin only)")
@app_commands.describe(minutes="Interval in minutes (1–1440)")
async def setinterval(interaction: discord.Interaction, minutes: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return
    if minutes < 1 or minutes > 1440:
        await interaction.response.send_message("⚠️ Range: 1–1440", ephemeral=True)
        return
    intervals_col.update_one({"guild_id": interaction.guild_id}, {"$set": {"interval": minutes}}, upsert=True)
    await interaction.response.send_message(f"✅ Interval set to {minutes} min.", ephemeral=True)

# --- Slash Command: List Linked Subreddits ---
@tree.command(name="listsubs", description="List all linked subreddits in this server")
async def listsubs(interaction: discord.Interaction):
    mappings = subs_col.find({"guild_id": interaction.guild_id})
    embed = discord.Embed(title="📄 Linked Subreddits", color=discord.Color.blurple())
    found = False
    for entry in mappings:
        found = True
        channel = bot.get_channel(entry["channel_id"])
        mention = f"<#{entry['channel_id']}>" if channel else "`unknown`"
        limit = entry.get("limit", 1)
        embed.add_field(name=f"r/{entry['subreddit']}", value=f"{mention} • Limit: {limit}", inline=False)
    if found:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("📭 No subreddits linked.", ephemeral=True)

# --- Slash Command: Send (With Cooldown & NSFW Check) ---
@tree.command(name="send", description="Send a post from the subreddit linked to this channel")
@commands.cooldown(rate=1, per=10, type=commands.BucketType.channel)
async def send(interaction: discord.Interaction):
    if not interaction.channel.is_nsfw():
        await interaction.response.send_message("⚠️ NSFW channels only.", ephemeral=True)
        return

    # Bypass cooldown for Admin/Mods
    if interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_messages:
        send.reset_cooldown(interaction)

    mapping = subs_col.find_one({
        "channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id
    })
    if not mapping:
        await interaction.response.send_message("❌ No subreddit linked to this channel.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    post = fetch_post(mapping["subreddit"])
    if post:
        embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
        embed.set_image(url=post["url"])
        embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send("❌ No content found.")

# --- Slash Command: Force Send (Admin Only) ---
@tree.command(name="forcesend", description="Force post from all subreddits to their channels (admin only)")
@app_commands.describe(count="Number of posts per mapping (1–5)")
async def forcesend(interaction: discord.Interaction, count: int = 1):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    if count < 1 or count > 5:
        await interaction.response.send_message("⚠️ Count must be between 1–5.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)

    sent_any = False
    for mapping in subs_col.find({"guild_id": interaction.guild_id}):
        channel = bot.get_channel(mapping["channel_id"])
        if not channel or not channel.is_nsfw():
            continue
        sent = 0
        while sent < count:
            post = fetch_post(mapping["subreddit"])
            if not post:
                break
            embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
            embed.set_image(url=post["url"])
            embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
            try:
                await channel.send(embed=embed)
                sent += 1
                sent_any = True
            except Exception as e:
                print(f"Send error in /forcesend: {e}")
                break

    if sent_any:
        await interaction.followup.send("✅ Posts sent.")
    else:
        await interaction.followup.send("❌ No posts could be sent.")

# --- Auto Posting Task (5 min loop) ---
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
            sent = 0
            while sent < post_limit:
                post = fetch_post(mapping["subreddit"])
                if not post:
                    break
                embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
                embed.set_image(url=post["url"])
                embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
                try:
                    await channel.send(embed=embed)
                    sent += 1
                except Exception as e:
                    print(f"Auto send error: {e}")
                    break

# --- On Ready Event ---
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
