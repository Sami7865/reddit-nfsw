import os import sys import types import asyncio import discord import random import logging import threading

from discord.ext import commands, tasks from discord import app_commands, Embed from flask import Flask from pymongo import MongoClient import asyncpraw import aiohttp

--- Audioop Patch for Python 3.13+ ---

if sys.version_info >= (3, 13): sys.modules['audioop'] = types.SimpleNamespace()

--- Logging ---

logging.basicConfig(level=logging.INFO)

--- Flask Keep-Alive for Render ---

app = Flask(name) @app.route('/') def home(): return "Bot is running!"

def run_flask(): app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run_flask).start()

--- Discord Setup ---

intents = discord.Intents.default() intents.message_content = True bot = commands.Bot(command_prefix="!", intents=intents) tree = bot.tree

OWNER_ID = 887243211645546517 LOG_CHANNEL_ID = 1391882689069580360 DEV_GUILD_ID = 1369650511208513636

--- MongoDB Setup ---

mongo = MongoClient(os.getenv("MONGO_URI")) db = mongo["nsfw_bot"] subs_col = db["subreddit_channels"] posts_col = db["sent_posts"] intervals_col = db["guild_intervals"] global_col = db["global_settings"]

--- Reddit Client (AsyncPRAW) ---

reddit = asyncpraw.Reddit( client_id=os.getenv("CLIENT_ID"), client_secret=os.getenv("CLIENT_SECRET"), user_agent=os.getenv("USER_AGENT"), username=os.getenv("REDDIT_USERNAME"), password=os.getenv("REDDIT_PASSWORD") )

GLOBAL_POST_INTERVAL = 10

--- Helper: Fetch Post ---

async def fetch_post(subreddit_name): try: subreddit = await reddit.subreddit(subreddit_name, fetch=True) posts = [post async for post in subreddit.hot(limit=50)] random.shuffle(posts)

for post in posts:
        if not post.over_18 or posts_col.find_one({"post_id": post.id}):
            continue

        url = ""
        if hasattr(post, "post_hint") and post.post_hint in ["image", "link", "hosted:video"]:
            url = post.url
        elif post.is_video and post.media and "reddit_video" in post.media:
            url = post.media["reddit_video"]["fallback_url"]

        if any(domain in post.url for domain in ["redgifs.com", "gfycat.com"]):
            url = post.url

        if url:
            posts_col.insert_one({"post_id": post.id})
            return {
                "title": post.title,
                "url": url,
                "permalink": f"https://reddit.com{post.permalink}",
                "score": post.score,
                "subreddit": subreddit.display_name
            }
except Exception as e:
    logging.warning(f"Fetch error for r/{subreddit_name}: {e}")
return None

--- Slash Commands ---

@tree.command(name="addsub", description="Link a subreddit to this channel") @app_commands.describe(subreddit="Subreddit name") async def addsub(interaction: discord.Interaction, subreddit: str): await interaction.response.defer(ephemeral=True, thinking=True)

async def validate():
    try:
        sub = await reddit.subreddit(subreddit, fetch=True)
        if sub.over18:
            subs_col.update_one(
                {"subreddit": sub.display_name.lower(), "channel_id": interaction.channel_id, "guild_id": interaction.guild_id},
                {"$set": {"subreddit": sub.display_name.lower()}}, upsert=True)
            await interaction.followup.send(f"✅ Linked r/{sub.display_name} to this channel.")
        else:
            await interaction.followup.send("❌ Subreddit is not marked NSFW.")
    except Exception:
        await interaction.followup.send("❌ Invalid or private subreddit.")

asyncio.create_task(validate())

@tree.command(name="removesub", description="Unlink subreddit from this channel") @app_commands.describe(subreddit="Subreddit name") async def removesub(interaction: discord.Interaction, subreddit: str): result = subs_col.delete_one({ "subreddit": subreddit.lower(), "channel_id": interaction.channel_id, "guild_id": interaction.guild_id }) msg = f"✅ Unlinked r/{subreddit}." if result.deleted_count else "❌ Subreddit not linked." await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="listsubs", description="List linked subreddits") async def listsubs(interaction: discord.Interaction): links = list(subs_col.find({"guild_id": interaction.guild_id})) if not links: await interaction.response.send_message("📭 No subreddits linked.", ephemeral=True) return embed = discord.Embed(title="📄 Linked Subreddits", color=discord.Color.purple()) for link in links: mention = f"<#{link['channel_id']}>" embed.add_field(name=f"r/{link['subreddit']}", value=mention, inline=False) await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="setinterval", description="Set auto-post interval (admin only)") @app_commands.describe(minutes="Minutes between posts (1–1440)") async def setinterval(interaction: discord.Interaction, minutes: int): if not interaction.user.guild_permissions.administrator: await interaction.response.send_message("❌ Admins only.", ephemeral=True) return if not (1 <= minutes <= 1440): await interaction.response.send_message("⚠️ Range: 1–1440", ephemeral=True) return intervals_col.update_one({"guild_id": interaction.guild_id}, {"$set": {"interval": minutes}}, upsert=True) await interaction.response.send_message(f"✅ Interval set to {minutes} minutes.", ephemeral=True)

@tree.command(name="setglobalinterval", description="Set global post interval (owner only)") @app_commands.describe(minutes="Global interval in minutes") async def setglobalinterval(interaction: discord.Interaction, minutes: int): if interaction.user.id != OWNER_ID: await interaction.response.send_message("❌ Owner only.", ephemeral=True) return global_col.update_one({"_id": "global"}, {"$set": {"interval": minutes}}, upsert=True) await interaction.response.send_message(f"✅ Global interval set to {minutes} minutes.", ephemeral=True)

@tree.command(name="send", description="Send a random post from the linked subreddit") async def send(interaction: discord.Interaction): if not interaction.channel.is_nsfw(): await interaction.response.send_message("⚠️ NSFW only.", ephemeral=True) return

mapping = subs_col.find_one({"channel_id": interaction.channel_id, "guild_id": interaction.guild_id})
if not mapping:
    await interaction.response.send_message("❌ No subreddit linked.", ephemeral=True)
    return

await interaction.response.defer()
post = await fetch_post(mapping["subreddit"])
if post:
    embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
    embed.set_image(url=post["url"])
    embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
    await interaction.followup.send(embed=embed)
else:
    await interaction.followup.send("❌ No suitable post found.")

@tree.command(name="forcesend", description="Force post from all mapped subs (admin only)") @app_commands.describe(count="Posts per subreddit (1–5)") async def forcesend(interaction: discord.Interaction, count: int = 1): if not interaction.user.guild_permissions.administrator: await interaction.response.send_message("❌ Admins only.", ephemeral=True) return if not (1 <= count <= 5): await interaction.response.send_message("⚠️ 1–5 only.", ephemeral=True) return await interaction.response.defer()

mappings = subs_col.find({"guild_id": interaction.guild_id})
total_sent = 0
for mapping in mappings:
    channel = bot.get_channel(mapping["channel_id"])
    if not channel or not channel.is_nsfw():
        subs_col.delete_one({"channel_id": mapping["channel_id"]})
        continue
    for _ in range(count):
        post = await fetch_post(mapping["subreddit"])
        if not post:
            break
        embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
        embed.set_image(url=post["url"])
        embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
        try:
            await channel.send(embed=embed)
            total_sent += 1
        except Exception as e:
            owner = await bot.fetch_user(OWNER_ID)
            await owner.send(f"❌ Error sending post in #{channel.name}: {e}")
await interaction.followup.send(f"✅ {total_sent} posts sent.")

--- Auto Posting Task ---

@tasks.loop(minutes=5) async def auto_post(): global GLOBAL_POST_INTERVAL doc = global_col.find_one({"_id": "global"}) if doc: GLOBAL_POST_INTERVAL = max(1, doc.get("interval", 10))

grouped = {}
for entry in subs_col.find():
    key = (entry["guild_id"], entry["channel_id"])
    grouped.setdefault(key, []).append(entry)

for (guild_id, channel_id), mappings in grouped.items():
    interval_doc = intervals_col.find_one({"guild_id": guild_id})
    interval = interval_doc["interval"] if interval_doc else GLOBAL_POST_INTERVAL
    if auto_post.current_loop % max(1, interval // 5) != 0:
        continue
    channel = bot.get_channel(channel_id)
    if not channel or not channel.is_nsfw():
        subs_col.delete_many({"channel_id": channel_id})
        continue
    for mapping in mappings:
        post = await fetch_post(mapping["subreddit"])
        if post:
            embed = discord.Embed(title=post["title"], url=post["permalink"], color=discord.Color.red())
            embed.set_image(url=post["url"])
            embed.set_footer(text=f"👍 {post['score']} • r/{post['subreddit']}")
            try:
                await channel.send(embed=embed)
            except Exception as e:
                owner = await bot.fetch_user(OWNER_ID)
                await owner.send(f"❌ Auto-post error in #{channel.name}: {e}")

--- Events ---

@bot.event async def on_ready(): print(f"✅ Logged in as {bot.user}") try: await tree.sync() await tree.sync(guild=discord.Object(id=DEV_GUILD_ID)) except Exception as e: logging.error(f"Command sync error: {e}") if not auto_post.is_running(): auto_post.start()

bot.run(os.getenv("DISCORD_TOKEN"))

