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
from datetime import datetime, timedelta, UTC
import aiohttp
from contextlib import asynccontextmanager

import discord
from discord import app_commands
from discord.ext import commands, tasks

from pymongo import MongoClient
import asyncpraw
from discord.errors import LoginFailure

# ─── Flask Keepalive Server ─────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

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
    print("Please set these in your Render environment settings at:")
    print("https://dashboard.render.com > Your Service > Environment")
    sys.exit(1)

# Print startup info
print("\n=== Bot Configuration ===")
print(f"Running on Render")
print(f"Reddit Username: {REDDIT_USERNAME}")
print(f"Reddit Client ID: {REDDIT_CLIENT_ID}")
print(f"MongoDB URI configured: {'Yes' if MONGO_URI else 'No'}")

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
sent_media_col = db["sent_media"]
stats_col = db["stats"]  # New collection for bot statistics

# Initialize MongoDB collections and indexes
async def init_mongodb():
    """Initialize MongoDB collections and indexes"""
    try:
        # Create TTL index for sent_media if it doesn't exist
        if "timestamp_1" not in sent_media_col.index_information():
            sent_media_col.create_index("timestamp", expireAfterSeconds=7 * 24 * 60 * 60)
            print("Created TTL index for sent_media collection")
            
        # Create indexes for faster lookups
        config_col.create_index("channel_id", unique=True)
        sent_media_col.create_index("url")
        stats_col.create_index("type")
        
        # Initialize or recover LAST_SENT from MongoDB
        global LAST_SENT
        stored_times = stats_col.find_one({"type": "last_sent"})
        if stored_times:
            LAST_SENT = {int(k): datetime.fromisoformat(v) for k, v in stored_times["data"].items()}
            print(f"Recovered timing data for {len(LAST_SENT)} channels")
        
        # Validate existing configs
        invalid_channels = []
        for cfg in config_col.find():
            channel_id = cfg.get("channel_id")
            if not channel_id:
                invalid_channels.append(cfg["_id"])
                continue
                
            # Ensure required fields exist
            updates = {}
            if "interval" not in cfg:
                updates["interval"] = GLOBAL_POST_INTERVAL
            if "subs" not in cfg:
                updates["subs"] = []
            if "added_at" not in cfg:
                updates["added_at"] = datetime.now(UTC)
            if "last_post_time" not in cfg:
                updates["last_post_time"] = datetime.min.replace(tzinfo=UTC)
                
            if updates:
                config_col.update_one({"_id": cfg["_id"]}, {"$set": updates})
        
        # Remove invalid configs
        if invalid_channels:
            config_col.delete_many({"_id": {"$in": invalid_channels}})
            print(f"Removed {len(invalid_channels)} invalid channel configurations")
            
        print("MongoDB initialization complete")
        return True
        
    except Exception as e:
        print(f"Error initializing MongoDB: {e}")
        return False

async def save_last_sent():
    """Save LAST_SENT times to MongoDB"""
    try:
        # Convert datetime objects to ISO format strings for MongoDB storage
        data = {str(k): v.isoformat() for k, v in LAST_SENT.items()}
        stats_col.update_one(
            {"type": "last_sent"},
            {"$set": {"data": data, "updated_at": datetime.now(UTC)}},
            upsert=True
        )
    except Exception as e:
        print(f"Error saving last sent times: {e}")

async def update_channel_stats(channel_id: int, post_url: str, subreddit: str):
    """Update channel posting statistics"""
    try:
        stats_col.update_one(
            {"type": "channel_stats", "channel_id": channel_id},
            {
                "$inc": {"total_posts": 1, f"subreddit_counts.{subreddit}": 1},
                "$set": {"last_post_url": post_url, "last_post_time": datetime.now(UTC)}
            },
            upsert=True
        )
    except Exception as e:
        print(f"Error updating channel stats: {e}")

# ─── Reddit Client ──────────────────────────────────────────────────────────────
# Global session variable
session = None
reddit = None

async def setup_reddit():
    """Setup Reddit client with proper session"""
    global session, reddit
    
    # Create custom session with proper configuration
    timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=10)
    session = aiohttp.ClientSession(timeout=timeout)
    
    # Initialize Reddit client with script-type user agent
    USER_AGENT = f"render:discord.nsfw.bot:v1.0 (by /u/{REDDIT_USERNAME})"
    print(f"User Agent: {USER_AGENT}")
    
    reddit = asyncpraw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent=USER_AGENT,
        requestor_kwargs={"session": session}
    )
    
    print("Reddit client initialized")

@asynccontextmanager
async def get_subreddit(name: str):
    """Safely get a subreddit with proper timeout handling."""
    if reddit is None:
        await setup_reddit()
    try:
        sub = await reddit.subreddit(name)
        yield sub
    except Exception as e:
        print(f"Error accessing subreddit r/{name}: {e}")
        raise

async def verify_subreddit_access(sub_name: str):
    """Verify if we can access a subreddit and log detailed error info."""
    try:
        print(f"\nTesting access to r/{sub_name}")
        
        async def _verify():
            async with get_subreddit(sub_name) as sub:
                async for post in sub.new(limit=1):
                    return True
            return False
            
        # Create and run task with timeout
        task = asyncio.create_task(_verify())
        result = await asyncio.wait_for(task, timeout=30.0)
        
        if result:
            print(f"Successfully accessed r/{sub_name}")
            return True, None
        else:
            print(f"No posts found in r/{sub_name}")
            return False, "Subreddit exists but has no posts"
            
    except asyncio.TimeoutError:
        print(f"Timeout accessing r/{sub_name}")
        return False, "Request timed out - please try again"
    except Exception as e:
        print(f"Error verifying access: {e}")
        return False, str(e)

async def fetch_post(subreddit: str):
    """Fetch a media post from the subreddit with variety."""
    try:
        async def _fetch():
            async with get_subreddit(subreddit) as sub:
                # Randomly choose a listing type
                listing_types = ['hot', 'new', 'top', 'rising']
                listing_type = listing_types[datetime.now(UTC).second % len(listing_types)]
                
                # For top posts, randomly choose a time filter
                time_filters = ['day', 'week', 'month', 'year', 'all']
                time_filter = time_filters[datetime.now(UTC).minute % len(time_filters)]
                
                # Get the appropriate listing
                if listing_type == 'top':
                    listing = sub.top(time_filter=time_filter, limit=100)  # Increased limit for more options
                else:
                    listing = getattr(sub, listing_type)(limit=100)
                
                valid_posts = []
                seen_urls = set()
                
                async for post in listing:
                    if post.stickied or post.is_self:
                        continue
                        
                    # Skip if we've seen this URL before
                    if post.url in seen_urls:
                        continue
                    seen_urls.add(post.url)
                    
                    # Skip if this media was sent in the last week
                    if await is_media_sent(post.url):
                        continue
                    
                    # Check for various media types
                    is_valid = False
                    
                    # Direct image links
                    if post.url.endswith((".jpg", ".png", ".gif", ".jpeg")):
                        is_valid = True
                    
                    # Reddit-hosted videos
                    elif "v.redd.it" in post.url and post.media:
                        if post.media.get("reddit_video", {}).get("fallback_url"):
                            is_valid = True
                    
                    # Redgifs links
                    elif "redgifs.com" in post.url or "gfycat.com" in post.url:
                        is_valid = True
                    
                    # Imgur links
                    elif "imgur.com" in post.url:
                        is_valid = True
                        
                    if is_valid:
                        valid_posts.append(post)
                        
                        # If we have enough posts, randomly select one
                        if len(valid_posts) >= 25:
                            selected_post = valid_posts[datetime.now(UTC).microsecond % len(valid_posts)]
                            await mark_media_sent(selected_post.url, selected_post.id, str(selected_post.subreddit))
                            return selected_post
                
                # If we have any valid posts, randomly select one
                if valid_posts:
                    selected_post = valid_posts[datetime.now(UTC).microsecond % len(valid_posts)]
                    await mark_media_sent(selected_post.url, selected_post.id, str(selected_post.subreddit))
                    return selected_post
                return None

        # Create and run task with timeout
        task = asyncio.create_task(_fetch())
        post = await asyncio.wait_for(task, timeout=30.0)
        
        if post:
            print(f"Fetched {post.url} from r/{subreddit} ({post.title[:30]}...)")
            return post
        print(f"No media posts found in r/{subreddit}")
        return None
        
    except asyncio.TimeoutError:
        print(f"Timeout fetching posts from r/{subreddit}")
        return None
    except Exception as e:
        print(f"Error fetching post: {e}")
        return None

# Add a command to clear the sent media history
@tree.command(
    name="clearmediahistory",
    description="Clear the sent media history (Admin only)"
)
async def clearmediahistory(interaction: discord.Interaction):
    if not interaction.user.id == BOT_OWNER_ID:
        return await interaction.response.send_message("❌ This command is only available to the bot owner.", ephemeral=True)
    
    try:
        result = sent_media_col.delete_many({})
        await interaction.response.send_message(f"✅ Cleared {result.deleted_count} entries from media history.")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error clearing media history: {e}", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

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

async def build_embed(post):
    """Build a rich embed for the post with enhanced media support."""
    try:
        embed = discord.Embed(
            title=post.title[:256],
            url=f"https://reddit.com{post.permalink}",
            description=f"👍 {post.score} | 💬 {post.num_comments}",
            timestamp=datetime.utcfromtimestamp(post.created_utc),
            color=discord.Color.red()
        )
        
        # Handle different types of media
        if "v.redd.it" in post.url and post.media:
            video_url = post.media.get("reddit_video", {}).get("fallback_url")
            if video_url:
                embed.add_field(name="Video", value=video_url, inline=False)
                # Add thumbnail if available
                if hasattr(post, 'thumbnail') and post.thumbnail != 'default':
                    embed.set_thumbnail(url=post.thumbnail)
        
        elif "redgifs.com" in post.url or "gfycat.com" in post.url:
            embed.add_field(name="GIF", value=post.url, inline=False)
            if hasattr(post, 'thumbnail') and post.thumbnail != 'default':
                embed.set_thumbnail(url=post.thumbnail)
        
        elif "imgur.com" in post.url:
            # Convert imgur links to direct images if possible
            if not post.url.endswith((".jpg", ".png", ".gif", ".jpeg")):
                if "/a/" not in post.url:  # Not an album
                    image_url = post.url + ".jpg"
                    embed.set_image(url=image_url)
            else:
                embed.set_image(url=post.url)
        
        elif post.url:  # Direct image links
            embed.set_image(url=post.url)
        
        embed.set_footer(text=f"Posted by u/{post.author} in r/{post.subreddit}")
        return embed
        
    except Exception as e:
        print(f"Error building embed: {e}")
        return None

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
    
    await interaction.response.defer(thinking=True)
    
    try:
        # Clean up subreddit name
        name = name.strip().lower()
        if name.startswith('r/'):
            name = name[2:]
            
        print(f"\nAttempting to add subreddit: r/{name}")
        
        # First verify we can access the subreddit
        can_access, error_msg = await verify_subreddit_access(name)
        if not can_access:
            print(f"Failed to verify access to r/{name}: {error_msg}")
            return await interaction.followup.send(
                f"❌ Could not access r/{name}.\n"
                f"Error: {error_msg}\n"
                "Please check:\n"
                "1. The subreddit name is spelled correctly\n"
                "2. The subreddit exists and is public\n"
                "3. The bot's Reddit account is properly configured for NSFW content\n"
                "4. Try again in a few moments if it was a timeout",
                ephemeral=True
            )
            
        # Try to fetch a post to verify we can get media content
        test_post = await fetch_post(name)
        if test_post is None:
            return await interaction.followup.send(
                f"❌ Could not find any media posts in r/{name}.\n"
                "Make sure the subreddit contains images or videos.",
                ephemeral=True
            )
            
        # Add to database
        config_col.update_one(
            {"channel_id": interaction.channel_id},
            {"$addToSet": {"subs": name}, "$setOnInsert": {
                "interval": GLOBAL_POST_INTERVAL,
                "limit": 25
            }},
            upsert=True
        )
        
        await interaction.followup.send(f"✅ Successfully added r/{name} to this channel!")
        
    except Exception as e:
        print(f"Error in addsub command for r/{name}: {e}")
        await interaction.followup.send(
            f"❌ An unexpected error occurred while adding r/{name}.\n"
            f"Error: {str(e)}\n"
            "Please try again or contact the bot owner if the issue persists.",
            ephemeral=True
        )
        await send_error_dm(BOT_OWNER_ID, f"Error in addsub for r/{name}: {str(e)}")

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
        await interaction.response.defer(thinking=True)
        
        cfg = get_config(interaction.channel_id)
        subs = cfg.get("subs", [])
        if not subs:
            return await interaction.followup.send("❌ No subreddits linked.")
        await interaction.followup.send("📜 Subreddits:\n" + "\n".join(f"- r/{s}" for s in subs))
    except Exception as e:
        print(f"Error in listsubs: {e}")
        await interaction.followup.send("❌ Error listing subreddits.", ephemeral=True)
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
    await interaction.response.defer(thinking=True)
    
    try:
        cfg = get_config(interaction.channel_id)
        subs = cfg.get("subs", [])
        if not subs:
            return await interaction.followup.send("❌ No subreddits linked.")
        
        sub = subs[datetime.now(UTC).second % len(subs)]
        post = await fetch_post(sub)
        if not post:
            return await interaction.followup.send("⚠️ No valid post found.")
        
        embed = await build_embed(post)
        if not embed:
            return await interaction.followup.send("⚠️ Failed to create embed.")
            
        await interaction.followup.send(embed=embed)
    except Exception as e:
        print(f"Error in send command: {e}")
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
    
    await interaction.response.defer(thinking=True)
    
    try:
        if not 1 <= count <= 5:
            return await interaction.followup.send("❌ Count must be between 1 and 5.", ephemeral=True)
        
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
                        if embed:
                            await channel.send(embed=embed)
                            success_count += 1
                except Exception as e:
                    print(f"Error in forcesend for r/{sub}: {e}")
                    fail_count += 1
                    continue
        
        await interaction.followup.send(f"✅ Force send complete!\nSuccess: {success_count}\nFailed: {fail_count}")
    except Exception as e:
        print(f"Error in forcesend command: {e}")
        await interaction.followup.send("❌ Error during force send.", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

# ─── Auto Poster Task ───────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def auto_post_loop():
    for cfg in config_col.find():
        try:
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
                embed = await build_embed(post)
                if embed:
                    await channel.send(embed=embed)
                    LAST_SENT[channel_id] = datetime.now(UTC)
                    await save_last_sent()
                    await update_channel_stats(channel_id, post.url, str(post.subreddit))
        except Exception as e:
            print(f"Error in auto_post_loop: {e}")
            continue

# Add stats command
@tree.command(
    name="channelstats",
    description="Show posting statistics for this channel"
)
async def channelstats(interaction: discord.Interaction):
    try:
        stats = stats_col.find_one({"type": "channel_stats", "channel_id": interaction.channel_id})
        if not stats:
            return await interaction.response.send_message("No statistics available for this channel yet.")
            
        total_posts = stats.get("total_posts", 0)
        sub_counts = stats.get("subreddit_counts", {})
        last_post_time = stats.get("last_post_time")
        
        # Build stats message
        msg = [
            "📊 **Channel Statistics**",
            f"Total posts: {total_posts}",
            "\nPosts by subreddit:"
        ]
        
        for sub, count in sorted(sub_counts.items(), key=lambda x: x[1], reverse=True):
            percentage = (count / total_posts) * 100
            msg.append(f"- r/{sub}: {count} ({percentage:.1f}%)")
            
        if last_post_time:
            msg.append(f"\nLast post: {last_post_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            
        await interaction.response.send_message("\n".join(msg))
    except Exception as e:
        await interaction.response.send_message(f"❌ Error fetching statistics: {e}", ephemeral=True)
        await send_error_dm(BOT_OWNER_ID, str(e))

# ─── Bot Events ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    try:
        print(f"Bot starting up as {bot.user.name}")
        
        # Initialize MongoDB
        if not await init_mongodb():
            print("WARNING: MongoDB initialization failed!")
            return
        
        # Setup Reddit client
        await setup_reddit()
        
        # Test Reddit auth
        auth_success = await test_reddit_auth()
        if not auth_success:
            print("WARNING: Reddit authentication test failed!")
            
        # Sync commands globally first
        await tree.sync()
        # Then sync to specific guild
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        auto_post_loop.start()
        
        logging_channel = bot.get_channel(LOGGING_CHANNEL_ID)
        if logging_channel:
            status = "✅" if auth_success else "⚠️"
            await logging_channel.send(
                f"{status} Bot restarted at {datetime.now(UTC)}\n"
                f"Reddit auth test: {'Success' if auth_success else 'Failed'}\n"
                f"MongoDB status: {'Initialized' if await init_mongodb() else 'Failed'}"
            )
        print("Bot is ready!")
    except Exception as e:
        print(f"Error during startup: {e}")
        if 'logging_channel' in locals() and logging_channel:
            await logging_channel.send(f"⚠️ Error during startup: {e}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Cooldown: Try again in {round(error.retry_after)}s", delete_after=5)
    else:
        await send_error_dm(BOT_OWNER_ID, str(error))

# ─── Cleanup ────────────────────────────────────────────────────────────────────
async def cleanup():
    """Cleanup resources before shutdown"""
    if session:
        await session.close()

async def test_reddit_auth():
    """Test Reddit authentication by attempting to access user info"""
    try:
        if reddit is None:
            await setup_reddit()
        me = await reddit.user.me()
        print(f"Reddit auth test successful - logged in as: {me.name}")
        return True
    except Exception as e:
        print(f"Reddit auth test failed: {e}")
        return False

# ─── Run Bot ────────────────────────────────────────────────────────────────────
async def start_bot():
    """Start the bot with proper error handling"""
    retries = 0
    max_retries = 5
    retry_delay = 60  # seconds

    while retries < max_retries:
        try:
            print(f"Starting bot (attempt {retries + 1}/{max_retries})...")
            await bot.start(TOKEN)
            break
        except LoginFailure as e:
            retries += 1
            print(f"Failed to login (attempt {retries}/{max_retries}): {e}")
            if retries < max_retries:
                wait_time = retry_delay * retries
                print(f"Waiting {wait_time} seconds before retrying...")
                await asyncio.sleep(wait_time)
            else:
                print("Max retries reached. Exiting...")
                sys.exit(1)
        except Exception as e:
            retries += 1
            print(f"Unexpected error (attempt {retries}/{max_retries}): {e}")
            if retries < max_retries:
                wait_time = retry_delay * retries
                print(f"Waiting {wait_time} seconds before retrying...")
                await asyncio.sleep(wait_time)
            else:
                print("Max retries reached. Exiting...")
                sys.exit(1)

def main():
    """Main entry point for the bot"""
    try:
        # Start Flask in a separate thread
        flask_thread = Thread(target=run_flask)
        flask_thread.daemon = True  # This ensures the Flask thread stops when the main program stops
        flask_thread.start()
        
        # Start the bot
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        print("Bot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
