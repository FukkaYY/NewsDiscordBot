import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler
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
import xml.etree.ElementTree as ET
from ddgs import DDGS

# Setup logging
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

# Save logs to bot.log, max size 1MB, backup count 3
file_handler = RotatingFileHandler('bot.log', maxBytes=1024*1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[console_handler, file_handler]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-3.5-flash")

# Initialize Gemini
genai.configure(api_key=GEMINI_API_KEY)


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
        self.available_models = []
        self.current_model_name = MODEL_NAME
        self.model = genai.GenerativeModel(model_name=MODEL_NAME)

    def load_available_models(self):
        try:
            if os.path.exists("models.md"):
                with open("models.md", "r", encoding="utf-8") as f:
                    self.available_models = [line.strip() for line in f if line.strip()]
            else:
                self.available_models = [MODEL_NAME]
                with open("models.md", "w", encoding="utf-8") as f:
                    f.write(MODEL_NAME + "\n")
        except Exception as e:
            logger.error(f"Error loading models.md: {e}")
            self.available_models = [MODEL_NAME]

    async def load_active_model(self):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT setting_value FROM system_settings WHERE setting_key = 'active_model'") as cursor:
                row = await cursor.fetchone()
                if row:
                    self.current_model_name = row[0]
                    self.model = genai.GenerativeModel(model_name=self.current_model_name)
                    logger.info(f"Loaded active model from DB: {self.current_model_name}")

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
                CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS news_history (
                    url TEXT PRIMARY KEY,
                    title TEXT,
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
        
        self.load_available_models()
        await self.load_active_model()
        
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
        try:
            await self.send_news_to_all_guilds()
            logger.info("Scheduled news delivery completed successfully.")
        except Exception as e:
            logger.error(f"Error in scheduled news task: {e}", exc_info=True)

    async def get_genre_scores(self) -> Dict[str, float]:
        # Get all genres first
        all_genres_str = self.get_genre_list()
        # Ensure we strip whitespace from each genre name
        all_genres = [g.strip() for g in all_genres_str.split(",") if g.strip()]
        # Initialize with default score of 3.0
        scores = {genre: 3.0 for genre in all_genres}
        
        try:
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
                    if genre is None:
                        continue
                    # Clean the genre name from DB just in case
                    genre = genre.strip()
                    weight = 1.0 / (max(0, days_passed) + 1.0)
                    if genre not in genre_stats:
                        genre_stats[genre] = {'weighted_sum': 0.0, 'sum_weights': 0.0}
                    genre_stats[genre]['weighted_sum'] += rating * weight
                    genre_stats[genre]['sum_weights'] += weight
                
                for genre, stats in genre_stats.items():
                    # Update existing score or add new genre from DB
                    if stats['sum_weights'] > 0:
                        scores[genre] = stats['weighted_sum'] / stats['sum_weights']
        except Exception as e:
            logger.error(f"Error in get_genre_scores: {e}")
        
        return scores

    async def send_news_to_all_guilds(self):
        logger.info("Starting send_news_to_all_guilds...")
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT guild_id, channel_id FROM config") as cursor:
                configs = await cursor.fetchall()
        
        if not configs:
            logger.info("No channels configured for news.")
            return

        genre_scores = await self.get_genre_scores()
        news_items = await self.fetch_news_with_gemini(genre_scores)
        if not news_items:
            logger.warning("No news items to send (either fetch failed or no new items).")
            return

        for guild_id, channel_id in configs:
            channel = self.bot_get_channel(channel_id)
            if not channel:
                try:
                    logger.info(f"Channel {channel_id} not in cache, fetching...")
                    channel = await self.fetch_channel(channel_id)
                except Exception as e:
                    logger.warning(f"Could not fetch channel {channel_id}: {e}")
                    continue

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

    def bot_get_channel(self, channel_id):
        return self.get_channel(channel_id)

    async def fetch_google_news_rss(self) -> List[Dict[str, Any]]:
        url = "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"
        raw_news = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.error(f"Failed to fetch Google News RSS: {response.status}")
                        return []
                    xml_data = await response.text()
            
            root = ET.fromstring(xml_data)
            items = root.findall(".//item")
            
            for item in items[:40]:
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                description = item.find("description").text if item.find("description") is not None else ""
                
                raw_news.append({
                    "title": title,
                    "body": description,
                    "url": link,
                    "source": "Google News RSS",
                    "date": pub_date
                })
        except Exception as e:
            logger.error(f"Error fetching Google News RSS: {e}")
        
        return raw_news

    async def fetch_news_with_gemini(self, genre_scores: Dict[str, float] = None) -> List[Dict[str, Any]]:
        today = datetime.datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y年%m月%d日")
        genre_list = self.get_genre_list()
        
        # Step 1: Fetch news using Google News RSS
        logger.info("Fetching news from Google News RSS...")
        raw_news = await self.fetch_google_news_rss()

        if not raw_news:
            logger.warning("No news found from Google News RSS.")
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
        5. 【重要】URLは必ず【ニュースリスト】にある元のURLをそのまま使用してください。勝手に生成したり、省略したりしないでください。
        6. 出力は必ず以下のJSON形式のみで返してください。
        
        [
            {{
                "title": "ニュースタイトル", 
                "url": "元のニュースのURL", 
                "summary": "短い要約",
                "genres": ["ジャンル1", "ジャンル2"]
            }},
            ...
        ]
        """
        
        try:
            response = await self.model.generate_content_async(prompt)
            if not response or not response.text:
                logger.error("Gemini returned an empty response.")
                return []

            text = response.text
            # Extract JSON block
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            
            try:
                items = json.loads(text)
            except json.JSONDecodeError as je:
                logger.error(f"Failed to parse Gemini response as JSON: {je}. Response text: {text}")
                return []

            if not isinstance(items, list):
                logger.error(f"Gemini returned JSON that is not a list: {type(items)}")
                return []
            
            # Save to history and return
            verified_items = []
            async with aiosqlite.connect(DB_FILE) as db:
                for item in items:
                    if not isinstance(item, dict) or 'url' not in item or 'title' not in item:
                        logger.warning(f"Skipping invalid item from Gemini: {item}")
                        continue
                    
                    if item['url'] in sent_urls:
                        continue
                    
                    # Ensure title is not None
                    if item['title'] is None:
                        item['title'] = "タイトルなし"

                    await db.execute("INSERT OR IGNORE INTO news_history (url, title) VALUES (?, ?)", (item['url'], item['title']))
                    # Save genres to news_genres table
                    for genre in item.get('genres', []):
                        if genre:
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

@bot.tree.command(name="interest", description="現在のジャンル別関心度を表示します")
async def interest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    genre_scores = await bot.get_genre_scores()
    
    if not genre_scores:
        await interaction.followup.send("関心度データがまだありません。", ephemeral=True)
        return

    # Sort genres by score descending
    sorted_genres = sorted(genre_scores.items(), key=lambda x: x[1], reverse=True)
    
    description = "【ユーザーの現在の関心度（5点満点）】\n"
    description += "※未評価のジャンルはデフォルトで3.00となります。\n\n"
    
    for genre, score in sorted_genres:
        # Create a visual bar [■■■□□]
        filled_blocks = min(5, max(0, int(round(score))))
        bar = "■" * filled_blocks + "□" * (5 - filled_blocks)
        description += f"- {genre}: `[{bar}]` **{score:.2f}**\n"
    
    embed = discord.Embed(
        title="📊 ジャンル別関心度",
        description=description,
        color=discord.Color.green()
    )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

# --- Model Management Commands ---
model_group = app_commands.Group(name="model", description="AIモデルの管理を行います")

@model_group.command(name="status", description="現在使用中のAIモデルを表示します")
async def model_status(interaction: discord.Interaction):
    await interaction.response.send_message(f"現在使用中のモデル: `{bot.current_model_name}`", ephemeral=True)

@model_group.command(name="list", description="利用可能なAIモデルの一覧を表示します")
async def model_list(interaction: discord.Interaction):
    models_str = "\n".join([f"- {m}" for m in bot.available_models])
    await interaction.response.send_message(f"**利用可能なモデル一覧:**\n{models_str}", ephemeral=True)

@model_group.command(name="set", description="使用するAIモデルを切り替えます")
@app_commands.describe(model_name="切り替え先のモデル名")
async def model_set(interaction: discord.Interaction, model_name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者のみ実行可能です。", ephemeral=True)
        return

    if model_name not in bot.available_models:
        await interaction.response.send_message(f"エラー: `{model_name}` は利用可能なモデルリストにありません。", ephemeral=True)
        return

    try:
        # Update bot instance
        bot.current_model_name = model_name
        bot.model = genai.GenerativeModel(model_name=model_name)
        
        # Save to DB
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR REPLACE INTO system_settings (setting_key, setting_value) VALUES (?, ?)",
                ("active_model", model_name)
            )
            await db.commit()
        
        await interaction.response.send_message(f"AIモデルを `{model_name}` に切り替えました。", ephemeral=True)
        logger.info(f"Model switched to {model_name} by {interaction.user}")
    except Exception as e:
        logger.error(f"Error switching model: {e}")
        await interaction.response.send_message(f"モデルの切り替え中にエラーが発生しました: {e}", ephemeral=True)

@model_set.autocomplete("model_name")
async def model_name_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=m, value=m)
        for m in bot.available_models if current.lower() in m.lower()
    ][:25]

bot.tree.add_command(model_group)

@bot.tree.command(name="genre_search", description="ジャンルを指定してニュースを検索します")
@app_commands.describe(genre="検索したいジャンル")
async def genre_search(interaction: discord.Interaction, genre: str):
    await interaction.response.defer()
    
    # 1. Search from history (DB)
    history_results = []
    async with aiosqlite.connect(DB_FILE) as db:
        query = """
            SELECT nh.title, nh.url, nh.sent_at
            FROM news_history nh
            JOIN news_genres ng ON nh.url = ng.url
            WHERE ng.genre = ? 
              AND nh.sent_at >= datetime('now', '-7 days')
            ORDER BY nh.sent_at DESC
            LIMIT 3
        """
        async with db.execute(query, (genre,)) as cursor:
            history_results = await cursor.fetchall()

    # 2. Search latest news (Web + Gemini)
    latest_results = []
    genre_list = bot.get_genre_list()
    try:
        with DDGS() as ddgs:
            search_query = f"{genre}"
            logger.info(f"Searching news for {genre} with timelimit='w'...")
            # Use .news() for better freshness and metadata
            results = ddgs.news(query=search_query, region="jp-jp", safesearch="on", timelimit="w", max_results=10)
            
            raw_web_news = []
            if results:
                for r in results:
                    raw_web_news.append({
                        "title": r.get("title"),
                        "body": r.get("body"),
                        "url": r.get("url"),
                        "source": r.get("source"),
                        "date": r.get("date")
                    })
            
            # Fallback to .text() if .news() returns nothing
            if not raw_web_news:
                logger.info("ddgs.news() returned no results, falling back to ddgs.text()...")
                results = ddgs.text(query=f"{genre} ニュース", region="jp-jp", safesearch="on", timelimit="w", max_results=10)
                raw_web_news = [{"title": r.get("body"), "url": r.get("href"), "original_title": r.get("title")} for r in results]

        if raw_web_news:
            prompt = f"""
            あなたはニュース選別アシスタントです。
            以下のニュースリストから、ジャンル「{genre}」に最も合致する【最新の重要なニュース】を最大3件選び、日本語で要約してください。
            
            【選定基準】
            1. 可能な限り「今日」または「数日前」の新しいニュースを優先してください。
            2. ジャンル「{genre}」との関連性が高いものを選んでください。
            3. 内容が重複している場合は、最も詳しいもの1つに絞ってください。

            また、各ニュースに対して、以下の【利用可能なジャンル】から最も適切なものを【最大3つ】選び、リスト（genres）として含めてください。
            
            【利用可能なジャンル】
            {genre_list}

            【ニュースリスト】
            {json.dumps(raw_web_news, ensure_ascii=False, indent=2)}
            
            【条件】
            1. URLは必ず【ニュースリスト】にある元のURLをそのまま使用してください。勝手に生成しないでください。

            【出力形式】必ず以下のJSON形式で返してください。
            [
                {{
                    "title": "ニュースタイトル",
                    "url": "元のニュースのURL",
                    "summary": "短い要約",
                    "genres": ["ジャンル1", "ジャンル2"]
                }},
                ...
            ]
            """
            response = await bot.model.generate_content_async(prompt)
            if response and response.text:
                text = response.text
                if "```json" in text:
                    text = text.split("```json")[1].split("```")[0].strip()
                elif "```" in text:
                    text = text.split("```")[1].split("```")[0].strip()
                latest_results = json.loads(text)
    except Exception as e:
        logger.error(f"Error in latest news search: {e}")

    # 3. Create Embed for history
    history_embed = discord.Embed(
        title=f"🔍 「{genre}」の検索結果（履歴）",
        color=discord.Color.blue(),
        timestamp=datetime.datetime.now()
    )

    history_text = ""
    if history_results:
        for title, url, sent_at in history_results:
            # Handle potential None title
            display_title = title if title else "タイトルなし"
            history_text += f"- [{display_title}]({url}) ({sent_at[:10]})\n"
    else:
        history_text = "過去の配信履歴に該当するニュースはありません。"
    history_embed.add_field(name="📌 過去の配信履歴", value=history_text, inline=False)

    await interaction.followup.send(embed=history_embed)

    # 4. Save and Send Latest News with RatingView
    if latest_results:
        async with aiosqlite.connect(DB_FILE) as db:
            for item in latest_results:
                # Save to history
                await db.execute("INSERT OR IGNORE INTO news_history (url, title) VALUES (?, ?)", (item['url'], item['title']))
                
                # Save all identified genres to news_genres table
                item_genres = item.get('genres', [])
                # Ensure the searched genre is included if not already there
                if genre not in item_genres:
                    item_genres.append(genre)
                
                for g_name in item_genres:
                    await db.execute("INSERT OR IGNORE INTO news_genres (url, genre) VALUES (?, ?)", (item['url'], g_name.strip()))
                
                await db.commit()

                # Send each latest news as a separate message with RatingView
                latest_embed = discord.Embed(
                    title=item['title'],
                    url=item['url'],
                    description=item['summary'],
                    color=discord.Color.blue()
                )
                genres_str = ", ".join(item_genres)
                latest_embed.set_footer(text=f"ジャンル: {genres_str} (最新検索)")
                
                view = RatingView(item['url'])
                await interaction.followup.send(embed=latest_embed, view=view)
    else:
        await interaction.followup.send(f"「{genre}」の最新ニュースは見つかりませんでした。")

@genre_search.autocomplete('genre')
async def genre_search_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    # Get all available genres
    try:
        with open("genres.md", "r", encoding="utf-8") as f:
            lines = f.readlines()
            genres = [line.strip("- \n") for line in lines if line.strip().startswith("-")]
    except:
        genres = ["政治", "経済", "社会", "国際", "テクノロジー", "科学", "ビジネス", "エンタメ", "スポーツ"]
    
    return [
        app_commands.Choice(name=genre, value=genre)
        for genre in genres if current.lower() in genre.lower()
    ][:25]

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

@bot.tree.command(name="period_search", description="期間を指定して、関心の高いニュースを検索します")
@app_commands.describe(
    start_date="開始日 (例: 2024-05-01)",
    end_date="終了日 (例: 2024-05-07)"
)
async def period_search(interaction: discord.Interaction, start_date: str, end_date: str):
    await interaction.response.defer()
    
    # 1. Parse Dates
    try:
        # Support common separators
        s_date = start_date.replace("/", "-").replace(".", "-")
        e_date = end_date.replace("/", "-").replace(".", "-")
        dt_start = datetime.datetime.strptime(s_date, "%Y-%m-%d")
        dt_end = datetime.datetime.strptime(e_date, "%Y-%m-%d")
        
        if dt_start > dt_end:
            await interaction.followup.send("開始日は終了日より前の日付を指定してください。")
            return
    except ValueError:
        await interaction.followup.send("日付の形式が正しくありません。YYYY-MM-DD 形式で入力してください。")
        return

    # 2. Get high interest genres
    genre_scores = await bot.get_genre_scores()
    sorted_genres = sorted(genre_scores.items(), key=lambda x: x[1], reverse=True)
    top_genres = [g for g, s in sorted_genres[:3]]
    genre_list = bot.get_genre_list()

    # 3. Web Search
    # We use the top genres as search keywords. 
    # DDGS timelimit doesn't support exact ranges, so we search and let Gemini filter.
    # We'll try to use a broad timelimit if possible, but 'y' (year) or 'm' (month) are the only options for older dates.
    # If the end_date is recent, we use appropriate timelimit.
    now = datetime.datetime.now()
    days_diff = (now - dt_start).days
    
    timelimit = None
    if days_diff <= 1: timelimit = "d"
    elif days_diff <= 7: timelimit = "w"
    elif days_diff <= 30: timelimit = "m"
    else: timelimit = "y"

    search_query = f"{' '.join(top_genres)} ニュース"
    logger.info(f"Period search for query: {search_query} (Target: {start_date} to {end_date})")
    
    raw_results = []
    try:
        with DDGS() as ddgs:
            results = ddgs.text(query=search_query, region="jp-jp", safesearch="on", timelimit=timelimit, max_results=20)
            raw_results = [{"title": r.get("body"), "url": r.get("href"), "original_title": r.get("title")} for r in results]
    except Exception as e:
        logger.error(f"Error in period web search: {e}")
        await interaction.followup.send("検索中にエラーが発生しました。")
        return

    if not raw_results:
        await interaction.followup.send(f"{start_date} から {end_date} の期間に該当するニュースは見つかりませんでした。")
        return

    # 4. Gemini Selection
    prompt = f"""
    あなたはニュース選別アシスタントです。
    ユーザーが指定した期間【{start_date} から {end_date}】に合致し、かつユーザーの関心が高いジャンル（{', '.join(top_genres)}）に関連する重要なニュースを最大3件選び、日本語で要約してください。

    【条件】
    1. ニュースの内容やURLから、指定された期間【{start_date} 〜 {end_date}】の出来事である可能性が高いものを優先してください。
    2. 以下の【利用可能なジャンル】から最も適切なものを【最大3つ】選び、リスト（genres）として含めてください。
    3. 重複を避け、重要度の高いものを厳選してください。
    4. URLは必ず【ニュースリスト】にある元のURLをそのまま使用してください。勝手に生成しないでください。

    【利用可能なジャンル】
    {genre_list}

    【ニュースリスト】
    {json.dumps(raw_results, ensure_ascii=False, indent=2)}

    【出力形式】必ず以下のJSON形式のみで返してください。
    [
        {{
            "title": "ニュースタイトル",
            "url": "元のニュースのURL",
            "summary": "30〜50文字程度の短い要約",
            "genres": ["ジャンル1", "ジャンル2"]
        }},
        ...
    ]
    """

    try:
        response = await bot.model.generate_content_async(prompt)

        if not response or not response.text:
            await interaction.followup.send("AIによる選定に失敗しました。")
            return

        text = response.text
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        
        selected_items = json.loads(text)
    except Exception as e:
        logger.error(f"Error processing Gemini response for period search: {e}")
        await interaction.followup.send("AIの回答を解析できませんでした。")
        return

    # 5. Result Display
    if not selected_items:
        await interaction.followup.send(f"指定された期間（{start_date}〜{end_date}）に合致する適切なニュースが見つかりませんでした。")
        return

    await interaction.followup.send(f"📅 **{start_date} 〜 {end_date}** の注目ニュースを表示します（関心ジャンル: {', '.join(top_genres)}）")

    async with aiosqlite.connect(DB_FILE) as db:
        for item in selected_items:
            # Save to history
            await db.execute("INSERT OR IGNORE INTO news_history (url, title) VALUES (?, ?)", (item['url'], item['title']))
            for g in item.get('genres', []):
                await db.execute("INSERT OR IGNORE INTO news_genres (url, genre) VALUES (?, ?)", (item['url'], g.strip()))
            await db.commit()

            embed = discord.Embed(
                title=item['title'],
                url=item['url'],
                description=item['summary'],
                color=discord.Color.dark_gold()
            )
            embed.set_footer(text=f"ジャンル: {', '.join(item.get('genres', []))}")
            
            view = RatingView(item['url'])
            await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="logs", description="Botの動作ログを確認します（管理者のみ）")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    lines="表示する直近のログ行数（デフォルト20、最大100）",
    download="ログファイルをダウンロードする場合はTrue（デフォルトFalse）"
)
async def get_logs(interaction: discord.Interaction, lines: int = 20, download: bool = False):
    await interaction.response.defer(ephemeral=True)
    
    if lines <= 0:
        lines = 20
    elif lines > 100:
        lines = 100
        
    log_file = "bot.log"
    
    if not os.path.exists(log_file):
        await interaction.followup.send("ログファイルが見つかりません。", ephemeral=True)
        return

    if download:
        try:
            file = discord.File(log_file, filename="bot.log")
            await interaction.followup.send("現在のログファイルです：", file=file, ephemeral=True)
        except Exception as e:
            logger.error(f"Error sending log file: {e}")
            await interaction.followup.send(f"ログファイルの送信中にエラーが発生しました: {e}", ephemeral=True)
    else:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            
            recent_lines = all_lines[-lines:]
            log_content = "".join(recent_lines)
            
            # Discord has a 2000 character limit. Keep it well under that limit.
            code_block_header = "```log\n"
            code_block_footer = "\n```"
            max_char = 1900
            
            if len(log_content) > max_char:
                log_content = log_content[-max_char:]
                lines_list = log_content.splitlines()
                if len(lines_list) > 1:
                    log_content = "\n".join(lines_list[1:]) + "\n(文字数制限のため、一部省略しました)"
            
            msg = f"直近の動作ログ（{len(recent_lines)}行分）です：\n{code_block_header}{log_content}{code_block_footer}"
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            logger.error(f"Error reading log file: {e}")
            await interaction.followup.send(f"ログファイルの読み込み中にエラーが発生しました: {e}", ephemeral=True)

@setup.error
@test_news.error
@get_logs.error
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
