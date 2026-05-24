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
from ddgs import DDGS

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-3.5-flash")

# Initialize Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(model_name=MODEL_NAME)


# Database file
DB_FILE = "news_bot.db"

class RatingView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.url = url

    async def save_rating(self, interaction: discord.Interaction, rating: int):
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR REPLACE INTO news_ratings (url, user_id, rating) VALUES (?, ?, ?)",
                (self.url, interaction.user.id, rating)
            )
            await db.commit()
        
        # Update button styles to show selection
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.label == str(rating) or child.label == f"評価: {rating}":
                    child.style = discord.ButtonStyle.green
                    child.label = f"評価: {rating}"
                else:
                    child.style = discord.ButtonStyle.gray
                    if child.label and child.label.startswith("評価: "):
                        child.label = child.label.replace("評価: ", "")
        
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="1", style=discord.ButtonStyle.gray)
    async def rate_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save_rating(interaction, 1)

    @discord.ui.button(label="2", style=discord.ButtonStyle.gray)
    async def rate_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save_rating(interaction, 2)

    @discord.ui.button(label="3", style=discord.ButtonStyle.gray)
    async def rate_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save_rating(interaction, 3)

    @discord.ui.button(label="4", style=discord.ButtonStyle.gray)
    async def rate_4(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save_rating(interaction, 4)

    @discord.ui.button(label="5", style=discord.ButtonStyle.gray)
    async def rate_5(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save_rating(interaction, 5)

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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS news_genres (
                    url TEXT,
                    genre TEXT,
                    FOREIGN KEY (url) REFERENCES news_history (url)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS news_ratings (
                    url TEXT,
                    user_id INTEGER,
                    rating INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (url, user_id),
                    FOREIGN KEY (url) REFERENCES news_history (url)
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

    def get_genre_list(self) -> str:
        try:
            with open("genres.md", "r", encoding="utf-8") as f:
                lines = f.readlines()
                # Extract bullet points (e.g., "- 政治 (Politics)")
                genres = [line.strip("- \n") for line in lines if line.strip().startswith("-")]
                return ", ".join(genres)
        except Exception as e:
            logger.error(f"Error reading genres.md: {e}")
            return "政治, 経済, 社会, 国際, テクノロジー, 科学, ビジネス, エンタメ, スポーツ"

    @tasks.loop(time=[datetime.time(hour=9, minute=0, tzinfo=ZoneInfo("Asia/Tokyo")), 
                      datetime.time(hour=17, minute=0, tzinfo=ZoneInfo("Asia/Tokyo"))])
    async def news_task(self):
        logger.info("Scheduled news delivery triggered.")
        await self.send_news_to_all_guilds()

    async def get_genre_scores(self) -> Dict[str, float]:
        # Get all genres first
        all_genres_str = self.get_genre_list()
        all_genres = [g.strip() for g in all_genres_str.split(",")]
        # Initialize with default score of 3.0
        scores = {genre: 3.0 for genre in all_genres}
        
        async with aiosqlite.connect(DB_FILE) as db:
            # Weighted average calculation: Weight = 1 / (days_passed + 1)
            query = """
                SELECT 
                    ng.genre,
                    nr.rating,
                    julianday('now') - julianday(nr.created_at) as days_passed
                FROM news_ratings nr
                JOIN news_genres ng ON nr.url = ng.url
            """
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                return scores
            
            genre_stats = {}
            for genre, rating, days_passed in rows:
                weight = 1.0 / (max(0, days_passed) + 1.0)
                if genre not in genre_stats:
                    genre_stats[genre] = {'weighted_sum': 0.0, 'sum_weights': 0.0}
                genre_stats[genre]['weighted_sum'] += rating * weight
                genre_stats[genre]['sum_weights'] += weight
            
            for genre, stats in genre_stats.items():
                if genre in scores:
                    scores[genre] = stats['weighted_sum'] / stats['sum_weights']
            
            return scores

    async def send_news_to_all_guilds(self):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT guild_id, channel_id FROM config") as cursor:
                configs = await cursor.fetchall()
        
        if not configs:
            logger.info("No channels configured for news.")
            return

        genre_scores = await self.get_genre_scores()
        news_items = await self.fetch_news_with_gemini(genre_scores)
        if not news_items:
            logger.error("Failed to fetch news items.")
            return

        for guild_id, channel_id in configs:
            channel = self.get_channel(channel_id)
            if channel:
                for item in news_items:
                    genres_str = ", ".join(item.get('genres', []))
                    embed = discord.Embed(
                        title=item['title'],
                        url=item['url'],
                        description=item.get('summary', "今日のニュースです。"),
                        color=discord.Color.blue()
                    )
                    if genres_str:
                        embed.add_field(name="ジャンル", value=genres_str, inline=False)
                    
                    # Attach RatingView
                    view = RatingView(item['url'])
                    await channel.send(embed=embed, view=view)
                logger.info(f"Sent news to guild {guild_id}, channel {channel_id}")
            else:
                logger.warning(f"Could not find channel {channel_id} for guild {guild_id}")

    async def fetch_news_with_gemini(self, genre_scores: Dict[str, float] = None) -> List[Dict[str, Any]]:
        today = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y年%m月%d日")
        genre_list = self.get_genre_list()
        
        # Step 1: Fetch news using DDGS
        logger.info("Fetching news from DuckDuckGo...")
        raw_news = []
        try:
            with DDGS() as ddgs:
                # Try .news() first
                try:
                    results = ddgs.news(query="最新ニュース", region="jp-jp", safesearch="on", timelimit="w", max_results=20)
                    for r in results:
                        raw_news.append({
                            "title": r.get("title"),
                            "body": r.get("body"),
                            "url": r.get("url"),
                            "source": r.get("source"),
                            "date": r.get("date")
                        })
                except Exception as ne:
                    logger.warning(f"DDGS .news() failed, trying .text(): {ne}")
                    # Fallback to .text() if .news() is rate-limited or fails
                    results = ddgs.text(query="最新ニュース", region="jp-jp", safesearch="on", timelimit="w", max_results=20)
                    for r in results:
                        raw_news.append({
                            "title": r.get("title"),
                            "body": r.get("body"),
                            "url": r.get("href"),
                            "source": "Search Result",
                            "date": today
                        })
        except Exception as e:
            logger.error(f"Error fetching from DuckDuckGo: {e}")
            return []

        if not raw_news:
            logger.warning("No news found from DuckDuckGo.")
            return []

        # Step 2 & 3: Selection and Summarization with Gemini
        logger.info(f"Processing {len(raw_news)} news items with Gemini...")
        
        # Format genre interest for prompt
        interest_info = ""
        high_interest_genres = []
        if genre_scores:
            sorted_genres = sorted(genre_scores.items(), key=lambda x: x[1], reverse=True)
            high_interest_genres = [g for g, s in sorted_genres[:3] if s >= 3.5]
            interest_info = "\n【ユーザーの現在の関心度（5点満点）】\n"
            for g, s in sorted_genres:
                interest_info += f"- {g}: {s:.2f}\n"
        
        # Get already sent URLs
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT url FROM news_history ORDER BY sent_at DESC LIMIT 100") as cursor:
                sent_urls = [row[0] for row in await cursor.fetchall()]

        # Filter out already sent URLs before sending to Gemini to save tokens
        filtered_raw_news = [n for n in raw_news if n['url'] not in sent_urls]
        
        if not filtered_raw_news:
            logger.warning("All fetched news items were already sent.")
            return []

        prompt = f"""
        今日は {today} です。
        以下のニュースリストから、重要度が高く、興味深い最新ニュースを【必ず5件】厳選し、日本語で要約とジャンル付与を行ってください。
        {interest_info}
        
        【ニュースリスト】
        {json.dumps(filtered_raw_news, ensure_ascii=False, indent=2)}
        
        【利用可能なジャンル】
        {genre_list}
        
        【条件】
        1. 重複がなく、最新のニュースとして相応しいものを【5件】選んでください。
        2. {"高関心ジャンル（" + ", ".join(high_interest_genres) + "）から【3件】、それ以外から【2件】選んでください。" if high_interest_genres else "バランスよく5件選んでください。"}
        3. 各ニュースについて、30〜50文字程度の短い要約（summary）を作成してください。
        4. 各ニュースに対し、上記の【利用可能なジャンル】から最も適切なものを【最大3つ】選び、リスト（genres）として含めてください。
        5. 出力は必ず以下のJSON形式のみで返してください。
        
        [
            {{
                "title": "ニュースタイトル", 
                "url": "URL", 
                "summary": "短い要約",
                "genres": ["ジャンル1", "ジャンル2"]
            }},
            ...
        ]
        """
        
        try:
            response = model.generate_content(prompt)
            if not response or not response.text:
                logger.error("Gemini returned an empty response.")
                return []

            text = response.text
            # Extract JSON block
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            
            items = json.loads(text)
            
            # Save to history and return
            verified_items = []
            async with aiosqlite.connect(DB_FILE) as db:
                for item in items:
                    if item['url'] in sent_urls:
                        continue
                    await db.execute("INSERT OR IGNORE INTO news_history (url) VALUES (?)", (item['url'],))
                    # Save genres to news_genres table
                    for genre in item.get('genres', []):
                        await db.execute("INSERT INTO news_genres (url, genre) VALUES (?, ?)", (item['url'], genre))
                    
                    verified_items.append(item)
                    if len(verified_items) >= 5:
                        break
                await db.commit()
            
            return verified_items
        except Exception as e:
            logger.error(f"Error processing news with Gemini: {e}")
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
