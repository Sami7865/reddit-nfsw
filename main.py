import os
import random
import discord
from discord.ext import tasks
from discord import app_commands
from pymongo import MongoClient
from flask import Flask
import praw
import threading

# Discord bot
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Flask keep-alive server
app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is alive!"
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8080)).start()

# Reddit API setup
reddit = praw.Reddit(
    client_id=os.getenv("CLIENT_ID"),
    client_secret=os.getenv("CLIENT_SECRET"),
    user_agent=os.getenv("USER_AGENT")
)

# MongoDB connection
mongo = MongoClient(os.getenv("MONGO_URI"))
db = mongo["nsfw_bot"]
subs_col = db["subreddit_channels"]
posts_col = db["sent_posts"]
intervals_col = db["guild_intervals"]

# Fetch Reddit post
def fetch_post(subreddit_name):
    subreddit = reddit.subreddit(subreddit_name)
    posts = list(subreddit.hot(limit=50))
    random.shuffle(posts)
    for post in posts:
        if not post.over_18 or posts_col.find_one({"post_id": post.id}):
            continue
        if post.url.endswith(('.jpg', '.png', '.gif', '.mp4', '.webm')):
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
    return None

# Slash: Add subreddit
@tree.command(name="addsub", description="Link a subreddit to this channel")
@app_commands.describe(subreddit="Subreddit name")
async def addsub(interaction: discord.Interaction, subreddit: str):
    entry = {
        "subreddit": subreddit,
        "channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id
    }
    if subs_col.find_one(entry):
        await interaction.response.send_message("⚠️ Already linked.", ephemeral=True)
    else:
        subs_col.insert_one(entry)
        await interaction.response.send_message(f"✅ Linked r/{subreddit} to this channel.", ephemeral=True)

# Slash: Remove subreddit
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

# Slash: Set post limit
@tree.command(name="setlimit", description="Set posts per cycle for a subreddit")
@app_commands.describe(subreddit="Subreddit name", count="Posts per cycle (1–10)")
async def setlimit(interaction: discord.Interaction, subreddit: str, count: int):
    if count < 1 or count > 10:
        await interaction.response.send_message("⚠️ Limit must be 1–10.", ephemeral=True)
        return
    query = {
        "subreddit": subreddit,
        "channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id
    }
    mapping = subs_col.find_one(query)
    if not mapping:
        await interaction.response.send_message("❌ Not linked.", ephemeral=True)
        return
    old = mapping.get("limit", 1)
    subs_col.update_one(query, {"$set": {"limit": count}})
    await interaction.response.send_message(
        f"✅ r/{subreddit} in <#{interaction.channel_id}> changed: {old} → {count}.",
        ephemeral=True
    )

# Slash: Set interval (per server)
@tree.command(name="setinterval", description="Set auto-post interval (admin only)")
@app_commands.describe(minutes="Minutes between posts (1–1440)")
async def setinterval(interaction: discord.Interaction, minutes: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return
    if minutes < 1 or minutes > 1440:
        await interaction.response.send_message("⚠️ 1–1440 min only.", ephemeral=True)
        return
    intervals_col.update_one(
        {"guild_id": interaction.guild_id},
        {"$set": {"interval": minutes}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Interval set to {minutes} min.", ephemeral=True)

# Slash: Send manually
@tree.command(name="send", description="Manually send post from a subreddit")
@app_commands.describe(subreddit="Subreddit name")
async def send(interaction: discord.Interaction, subreddit: str):
    await interaction.response.defer(thinking=True)
    if not interaction.channel.is_nsfw():
        await interaction.followup.send("⚠️ NSFW only.")
        return
    post = fetch_post(subreddit)
    if post:
        embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
        embed.description = f"[🔗 Media Link]({post['url']})"
        embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send("❌ No content found.")

# Slash: Force post
@tree.command(name="forcepost", description="Force send from a linked subreddit")
@app_commands.describe(subreddit="Subreddit name")
async def forcepost(interaction: discord.Interaction, subreddit: str):
    entry = subs_col.find_one({"subreddit": subreddit, "guild_id": interaction.guild_id})
    if not entry:
        await interaction.response.send_message("❌ No mapping found.", ephemeral=True)
        return
    channel = bot.get_channel(entry["channel_id"])
    if not channel or not channel.is_nsfw():
        await interaction.response.send_message("⚠️ Invalid channel.", ephemeral=True)
        return
    post = fetch_post(subreddit)
    if post:
        embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
        embed.description = f"[🔗 Media Link]({post['url']})"
        embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
        await channel.send(embed=embed)
        await interaction.response.send_message("✅ Post sent.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ No content found.", ephemeral=True)

# Slash: List linked subreddits
@tree.command(name="listsubs", description="List all linked subreddits")
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

# Auto post task
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
                embed.description = f"[🔗 Media Link]({post['url']})"
                embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
                try:
                    await channel.send(embed=embed)
                    sent += 1
                except Exception as e:
                    print(f"Send error: {e}")
                    break

# Ready and sync
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    dev_guild = discord.Object(id=1369650511208513636)
    try:
        synced = await tree.sync()
        print(f"🌍 Global commands: {len(synced)}")
    except Exception as e:
        print(f"❌ Global sync error: {e}")
    try:
        dev_synced = await tree.sync(guild=dev_guild)
        print(f"🛠️ Synced {len(dev_synced)} to dev guild.")
    except Exception as e:
        print(f"❌ Dev guild sync error: {e}")
    if not auto_post.is_running():
        auto_post.start()

bot.run(os.getenv("DISCORD_TOKEN"))
