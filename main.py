import os
import asyncio
import logging
import datetime
import json
from zoneinfo import ZoneInfo
from typing import List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import google.generativeai as genai
import aiosqlite
import aiohttp

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Initialize Gemini
genai.configure(api_key=GEMINI_API_KEY)
# Enable Google Search grounding using the latest flagship model
model = genai.GenerativeModel(
    model_name='gemini-3.5-flash',
    tools=[{"google_search_retrieval": {}}]
)

# Database file
DB_FILE = "news_bot.db"

class NewsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Initialize Database
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS news_history (
                    url TEXT PRIMARY KEY,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()
        
        # Sync slash commands
        await self.tree.sync()
        logger.info("Slash commands synced.")
        
        # Start scheduled tasks
        self.news_task.start()

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

    @tasks.loop(time=[datetime.time(hour=9, minute=0, tzinfo=ZoneInfo("Asia/Tokyo")), 
                      datetime.time(hour=17, minute=0, tzinfo=ZoneInfo("Asia/Tokyo"))])
    async def news_task(self):
        logger.info("Scheduled news delivery triggered.")
        await self.send_news_to_all_guilds()

    async def send_news_to_all_guilds(self):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT guild_id, channel_id FROM config") as cursor:
                configs = await cursor.fetchall()
        
        if not configs:
            logger.info("No channels configured for news.")
            return

        news_items = await self.fetch_news_with_gemini()
        if not news_items:
            logger.error("Failed to fetch news items.")
            return

        for guild_id, channel_id in configs:
            channel = self.get_channel(channel_id)
            if channel:
                for item in news_items:
                    embed = discord.Embed(
                        title=item['title'],
                        url=item['url'],
                        description="今日のニュースです。",
                        color=discord.Color.blue()
                    )
                    await channel.send(embed=embed)
                logger.info(f"Sent news to guild {guild_id}, channel {channel_id}")
            else:
                logger.warning(f"Could not find channel {channel_id} for guild {guild_id}")

    async def fetch_news_with_gemini(self) -> List[Dict[str, str]]:
        today = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y年%m月%d日")
        prompt = f"""
        今日は {today} です。
        今日の最新ニュースを5件ピックアップしてください。
        
        【重要：検証ステップ】
        選んだ各ニュースについて、以下の2点を厳格に確認してください：
        1. そのニュースが本当に {today} のものであること。
        2. そのURLが現在実在し、正しくアクセスできること。
        
        もし確信が持てない場合は、別の確実なニュースを探してください。
        
        出力は必ず以下のJSON形式で返してください。
        [
            {{"title": "ニュースのタイトル1", "url": "ニュースのURL1"}},
            {{"title": "ニュースのタイトル2", "url": "ニュースのURL2"}},
            ...
        ]
        """
        
        try:
            # Get already sent URLs
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT url FROM news_history ORDER BY sent_at DESC LIMIT 100") as cursor:
                    sent_urls = [row[0] for row in await cursor.fetchall()]
            
            if sent_urls:
                prompt += f"\n以下のURLは既に送信済みなので除外してください:\n" + "\n".join(sent_urls)

            response = model.generate_content(prompt)
            if not response or not response.text:
                logger.error("Gemini returned an empty response.")
                return []

            text = response.text
            logger.debug(f"Raw Gemini response: {text}")
            
            # Extract JSON block
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            
            try:
                items = json.loads(text)
            except json.JSONDecodeError as je:
                logger.error(f"Failed to decode JSON from Gemini: {je}. Raw text: {text}")
                return []
            
            # URL Validation Phase
            verified_items = []
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            async with aiohttp.ClientSession(headers=headers) as session:
                for item in items:
                    if not isinstance(item, dict) or 'url' not in item or 'title' not in item:
                        continue
                    if item['url'] in sent_urls:
                        continue
                        
                    try:
                        async with session.get(item['url'], timeout=5) as resp:
                            if resp.status == 200:
                                verified_items.append(item)
                                logger.info(f"Verified URL: {item['url']}")
                            else:
                                logger.warning(f"Invalid URL (Status {resp.status}): {item['url']}")
                    except Exception as e:
                        logger.warning(f"Could not verify URL {item['url']}: {e}")
            
            # Save to history and return
            new_items = []
            async with aiosqlite.connect(DB_FILE) as db:
                for item in verified_items:
                    await db.execute("INSERT OR IGNORE INTO news_history (url) VALUES (?)", (item['url'],))
                    new_items.append(item)
                    if len(new_items) >= 5:
                        break
                await db.commit()
            
            return new_items[:5]
        except Exception as e:
            logger.error(f"Error fetching news from Gemini: {e}")
            return []

bot = NewsBot()

@bot.tree.command(name="setup", description="ニュースを受信するチャンネルを設定します（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO config (guild_id, channel_id) VALUES (?, ?)",
            (interaction.guild_id, interaction.channel_id)
        )
        await db.commit()
    
    await interaction.response.send_message(
        f"ニュース受信チャンネルを {interaction.channel.mention} に設定しました。",
        ephemeral=True
    )

@bot.tree.command(name="test_news", description="ニュース配信を今すぐテストします（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
async def test_news(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await bot.send_news_to_all_guilds()
    await interaction.followup.send("ニュース配信テストを完了しました。", ephemeral=True)

@setup.error
@test_news.error
async def admin_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("このコマンドを実行するには管理者権限が必要です。", ephemeral=True)
    else:
        await interaction.response.send_message(f"エラーが発生しました: {error}", ephemeral=True)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not GEMINI_API_KEY:
        logger.error("DISCORD_BOT_TOKEN or GEMINI_API_KEY is not set in .env file.")
    else:
        bot.run(DISCORD_TOKEN)
