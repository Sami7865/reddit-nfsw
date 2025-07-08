import os
import discord
import asyncio
import random
import aiohttp
import logging
import audioop
from discord.ext import tasks
from discord import app_commands
from flask import Flask
from pymongo import MongoClient
import asyncpraw

# Credentials via Render environment variables
TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT")

OWNER_ID = 887243211645546517
LOG_CHANNEL_ID = 1391882689069580360
GUILD_ID = 1369650511208513636

# Discord bot client
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Reddit async client
reddit = asyncpraw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    user_agent=REDDIT_USER_AGENT
)

# MongoDB setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["reddit"]
subs_col = db["subs"]
config_col = db["config"]

# Global interval default
GLOBAL_POST_INTERVAL = 30

# Flask app for uptime
app = Flask("")

@app.route("/")
def home():
    return "Bot is running!"

# DM error messages to owner
async def send_owner_dm(message):
    try:
        owner = await client.fetch_user(OWNER_ID)
        await owner.send(f"⚠️ Error:\n```{message}```")
    except:
        pass

def is_admin_or_mod(interaction: discord.Interaction):
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild or perms.manage_channels

# Fetch Reddit post
async def fetch_post(subreddit_name, limit):
    try:
        subreddit = await reddit.subreddit(subreddit_name)
        await subreddit.load()
        submissions = [s async for s in subreddit.hot(limit=limit)]
        posts = [s for s in submissions if not s.stickied and hasattr(s, "url")]
        return random.choice(posts) if posts else None
    except Exception as e:
        await send_owner_dm(f"Error fetching r/{subreddit_name}: {e}")
        return None

# Send post to channel
async def send_subreddit_post(channel_id, subreddit, limit):
    channel = client.get_channel(channel_id)
    if not channel:
        subs_col.delete_many({"channel_id": channel_id})
        return
    post = await fetch_post(subreddit, limit)
    if not post:
        return
    embed = discord.Embed(title=post.title, url=f"https://reddit.com{post.permalink}", color=0xFF005F)
    if post.url.endswith(("jpg", "jpeg", "png", "gif")):
        embed.set_image(url=post.url)
    embed.set_footer(text=f"From r/{subreddit}")
    try:
        await channel.send(embed=embed)
        log_channel = client.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"✅ Posted in <#{channel.id}> from r/{subreddit}")
    except discord.HTTPException as e:
        await send_owner_dm(f"Failed to send embed to {channel.id} (r/{subreddit}): {e}")

# Slash Commands

@tree.command(name="send", description="Send a Reddit post from the linked subreddit")
@app_commands.checks.cooldown(1, 10)
async def send(interaction: discord.Interaction):
    data = subs_col.find_one({"channel_id": interaction.channel.id})
    if not data:
        await interaction.response.send_message("⚠️ No subreddit linked to this channel.", ephemeral=True)
        return
    await interaction.response.defer()
    await send_subreddit_post(interaction.channel.id, data["subreddit"], data.get("limit", 50))
    await interaction.followup.send("✅ Post sent.", ephemeral=True)

@tree.command(name="forcesend", description="Force post from all linked subreddits")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(count="Number of posts per channel")
async def forcesend(interaction: discord.Interaction, count: int = 1):
    await interaction.response.defer(ephemeral=True)
    all_links = subs_col.find()
    for data in all_links:
        for _ in range(count):
            await send_subreddit_post(data["channel_id"], data["subreddit"], data.get("limit", 50))
    await interaction.followup.send("✅ Forced post complete.", ephemeral=True)

@tree.command(name="addsub", description="Link a subreddit to this channel")
@app_commands.describe(subreddit="The subreddit to link")
async def addsub(interaction: discord.Interaction, subreddit: str):
    try:
        sub = await reddit.subreddit(subreddit)
        await sub.load()
        if not sub.over18:
            await interaction.response.send_message("⚠️ This subreddit is not marked NSFW.", ephemeral=True)
            return
        subs_col.update_one(
            {"channel_id": interaction.channel.id},
            {"$set": {"subreddit": subreddit.lower(), "limit": 50}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ Linked r/{subreddit} to this channel.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Invalid subreddit: `{e}`", ephemeral=True)

@tree.command(name="removesub", description="Unlink subreddit from this channel")
async def removesub(interaction: discord.Interaction):
    subs_col.delete_one({"channel_id": interaction.channel.id})
    await interaction.response.send_message("✅ Unlinked subreddit from this channel.", ephemeral=True)

@tree.command(name="listsubs", description="List all subreddit links")
@app_commands.checks.has_permissions(administrator=True)
async def listsubs(interaction: discord.Interaction):
    data = list(subs_col.find())
    if not data:
        await interaction.response.send_message("⚠️ No subreddits linked yet.", ephemeral=True)
        return
    msg = "\n".join([f"<#{d['channel_id']}> → r/{d['subreddit']}" for d in data])
    await interaction.response.send_message(f"📄 Linked Subreddits:\n{msg}", ephemeral=True)

@tree.command(name="setlimit", description="Set how many posts to pull from subreddit")
@app_commands.checks.has_permissions(administrator=True)
async def setlimit(interaction: discord.Interaction, limit: int):
    if limit < 1 or limit > 100:
        await interaction.response.send_message("⚠️ Limit must be between 1 and 100.", ephemeral=True)
        return
    subs_col.update_one({"channel_id": interaction.channel.id}, {"$set": {"limit": limit}})
    await interaction.response.send_message(f"✅ Limit set to {limit} for this channel.", ephemeral=True)

@tree.command(name="setinterval", description="Set posting interval for this channel")
@app_commands.checks.has_permissions(administrator=True)
async def setinterval(interaction: discord.Interaction, minutes: int):
    subs_col.update_one({"channel_id": interaction.channel.id}, {"$set": {"interval": minutes}})
    await interaction.response.send_message(f"✅ Posting interval set to {minutes} minutes for this channel.", ephemeral=True)

@tree.command(name="setglobalinterval", description="Set global post interval (all channels)")
@app_commands.checks.has_permissions(administrator=True)
async def setglobalinterval(interaction: discord.Interaction, minutes: int):
    config_col.update_one({"_id": "global"}, {"$set": {"interval": minutes}}, upsert=True)
    await interaction.response.send_message(f"✅ Global interval set to {minutes} minutes.", ephemeral=True)

# Error handler
@client.event
async def on_app_command_error(interaction, error):
    if isinstance(error, app_commands.errors.CommandOnCooldown):
        await interaction.response.send_message(f"⏳ Cooldown: try again in {round(error.retry_after, 1)}s.", ephemeral=True)
    elif isinstance(error, app_commands.errors.CheckFailure):
        await interaction.response.send_message("🚫 You don't have permission to use this command.", ephemeral=True)
    else:
        await send_owner_dm(f"Unhandled error: {error}")

# On ready
@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    await tree.sync()
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    autopost.start()

# Autopost loop
@tasks.loop(minutes=1)
async def autopost():
    global GLOBAL_POST_INTERVAL
    config = config_col.find_one({"_id": "global"})
    if config:
        GLOBAL_POST_INTERVAL = config.get("interval", 30)

    for doc in subs_col.find():
        interval = doc.get("interval", GLOBAL_POST_INTERVAL)
        if random.randint(1, interval) == 1:
            await send_subreddit_post(doc["channel_id"], doc["subreddit"], doc.get("limit", 50))

# Flask keep-alive + Discord run
if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8080)).start()
    client.run(TOKEN)
