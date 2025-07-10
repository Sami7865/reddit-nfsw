# ─── Reddit Client ──────────────────────────────────────────────────────────────────
import aiohttp
import asyncio
from contextlib import asynccontextmanager

# Initialize Reddit client with Render-specific user agent
USER_AGENT = f"script:discord.nsfw.bot:v1.0 (by /u/{REDDIT_USERNAME})"
print(f"User Agent: {USER_AGENT}")

# Global session variable
session = None
reddit = None

async def setup_reddit():
    """Setup Reddit client with proper session"""
    global session, reddit
    
    # Create custom session with proper configuration
    timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=10)
    session = aiohttp.ClientSession(timeout=timeout)
    
    # Initialize Reddit client
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

# Rest of the subreddit functions stay the same...

# ─── Bot Events ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    try:
        print(f"Bot starting up as {bot.user.name}")
        
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
                f"Reddit auth test: {'Success' if auth_success else 'Failed'}"
            )
        print("Bot is ready!")
    except Exception as e:
        print(f"Error during startup: {e}")
        if 'logging_channel' in locals() and logging_channel:
            await logging_channel.send(f"⚠️ Error during startup: {e}")

# ─── Cleanup ────────────────────────────────────────────────────────────────────
async def cleanup():
    """Cleanup resources before shutdown"""
    if session:
        await session.close()

# ─── Run Bot ────────────────────────────────────────────────────────────────────
MAX_RETRIES = 5
RETRY_DELAY = 60  # seconds

async def start_bot():
    retries = 0
    try:
        while retries < MAX_RETRIES:
            try:
                print(f"Starting bot (attempt {retries + 1}/{MAX_RETRIES})...")
                await bot.start(TOKEN)
                break
            except LoginFailure as e:
                retries += 1
                print(f"Failed to login (attempt {retries}/{MAX_RETRIES}): {e}")
                if retries < MAX_RETRIES:
                    wait_time = RETRY_DELAY * retries
                    print(f"Waiting {wait_time} seconds before retrying...")
                    await asyncio.sleep(wait_time)
                else:
                    print("Max retries reached. Exiting...")
                    sys.exit(1)
            except Exception as e:
                retries += 1
                print(f"Unexpected error (attempt {retries}/{MAX_RETRIES}): {e}")
                if retries < MAX_RETRIES:
                    wait_time = RETRY_DELAY * retries
                    print(f"Waiting {wait_time} seconds before retrying...")
                    await asyncio.sleep(wait_time)
                else:
                    print("Max retries reached. Exiting...")
                    sys.exit(1)
    finally:
        await cleanup()

def run_bot():
    """Run the bot with proper async handling"""
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        print("Bot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # Start Flask in a separate thread
    Thread(target=run_flask).start()
    # Run the bot
    run_bot()
