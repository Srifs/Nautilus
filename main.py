import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from dotenv import load_dotenv
import os
import aiohttp
import aiosqlite
import sqlite3
import time
from logging.handlers import RotatingFileHandler


load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MCC_API_KEY = os.getenv("MCC_API_KEY")
MCC_API_URL = "https://api.mccisland.net/graphql"

handler = RotatingFileHandler("discord.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)

DB_PATH = "bot.db"

http_session = None
games_cache = []
game_status_cache = {}
live_status_messages = {}
GAME_DETAILS = {
    "battle_box_arena": {"name": "Battle Box Arena", "emoji": 1539777268388331581},
    "battle_box_quads": {"name": "Battle Box", "emoji": 1539777319978278942},
    "dynaball": {"name": "Dynaball", "emoji": 1539777244468084867},
    "hole_in_the_wall": {"name": "Hole In The Wall", "emoji": 1539777355743236116},
    "parkour_warrior_survival": {"name": "Parkour Warrior Survivor", "emoji": 1539777370590814278},
    "rocket_spleef": {"name": "Rocket Spleef Rush", "emoji": 1539777391499546716},
    "sky_battle_quads": {"name": "Sky Battle", "emoji": 1539777289372569750},
    "sky_battle_solos": {"name": "Sky Battle Solo", "emoji": 1539779001609093191},
    "tgttos": {"name": "TGTTOS", "emoji": 1539777338168971315}
}

class Bot(commands.Bot):
    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        self.db = await aiosqlite.connect(DB_PATH)
        await self.setup_database()

    async def setup_database(self):
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS live_status_messages (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                game TEXT NOT NULL
            )
        """)

        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS game_player_counts (
                timestamp INTEGER NOT NULL,
                game TEXT NOT NULL,
                player_count INTEGER NOT NULL
            )
        """)

        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_player_counts_game_timestamp
            ON game_player_counts(game, timestamp)
        """)

        await self.db.commit()

        async with self.db.execute("PRAGMA journal_mode") as cursor:
            print("Journal mode:", await cursor.fetchone())

        async with self.db.execute("PRAGMA synchronous") as cursor:
            print("Synchronous:", await cursor.fetchone())

        async with self.db.execute("PRAGMA page_count") as cursor:
            print("Page count:", await cursor.fetchone())

        async with self.db.execute("PRAGMA max_page_count") as cursor:
            print("Max page count:", await cursor.fetchone())


    async def add_game_player_records(self, db_records):
        await self.db.executemany(
            """
            INSERT INTO game_player_counts
            (timestamp, game, player_count)
            VALUES (?, ?, ?)
            """,
            db_records
        )

        print("saved player count history")

        await self.db.commit()

    async def add_live_status_message(self, message_info_record):
        await self.db.execute(
            """
            INSERT INTO live_status_messages
            (guild_id, channel_id, message_id, game)
            VALUES (?, ?, ?, ?)
            """,
            message_info_record
        )

        print("saved live message instance")

        await self.db.commit()

    async def delete_live_status_message(self, message_id):
        await self.db.execute(
            """
            DELETE FROM live_status_messages
            WHERE message_id = ?
            """,
            (message_id,)
        )
        await self.db.commit()

    async def load_live_status_messages(self):
        async with self.db.execute("SELECT guild_id, channel_id, message_id, game FROM live_status_messages") as cursor:
            rows = await cursor.fetchall()

        for guild_id, channel_id, message_id, game in rows:
            channel = self.get_channel(channel_id)

            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except discord.NotFound:
                    print(f"Channel {channel_id} no longer exists")
                    continue
                except discord.Forbidden:
                    print(f"Cannot access channel {channel_id}")
                    continue

            try:
                message = await channel.fetch_message(message_id)

                live_status_messages[message_id] = {"message": message,"game": game}

            except discord.NotFound:
                print(f"Live status message {message_id} no longer exists")
                await bot.delete_live_status_message(message_id)

            except discord.Forbidden:
                print(f"Cannot access live status message {message_id}")

        print(f"Loaded {len(live_status_messages)} live status messages")

    async def close(self):
        await self.http_session.close()
        await self.db.close()
        await super().close()

bot = Bot(command_prefix="/", intents=intents)

@bot.event
async def on_ready():
    print(f"{bot.user} is up")

    try:
        await update_games()
        await bot.load_live_status_messages()

        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")

        if not update_live_statuses.is_running():
            update_live_statuses.start()

    except Exception as e:
        print(f"Failed to sync commands: {e}")

async def update_games():
    global games_cache
    query = """query availableQueueTypes { availableQueueTypes }"""

    try:
        data = await mcci_query(query)
        games_cache = data["availableQueueTypes"]

    except Exception as e:
        print(f"Failed to load game types: {e}")

async def mcci_query(query: str, variables: dict | None = None):
    headers = {"X-API-Key": MCC_API_KEY, "Content-Type": "application/json"}
    payload = {"query": query, "variables": variables or {}}

    async with bot.http_session.post(MCC_API_URL, json=payload, headers=headers) as response:
            data = await response.json()

            if response.status != 200:
                raise Exception(f"API returned HTTP {response.status}: {data}")
            if "errors" in data:
                raise Exception(f"GraphQL error: {data['errors']}")
            return data["data"]

async def update_game_status_cache():
    if not games_cache:
        return

    fields = []
    variables = {}

    for index, game in enumerate(games_cache):
        variable_name = f"game{index}"
        alias_name = f"game{index}"
        variables[variable_name] = game
        fields.append(f"{alias_name}: playerCount(queueType: ${variable_name})")
        fields.append(f"{alias_name}_popularity: popularity(queueType: ${variable_name})")

    variable_definitions = ", ".join(f"${name}: String!" for name in variables)

    query = f"""query QueueStats({variable_definitions}) {{{" ".join(fields)}}}"""

    try:
        data = await mcci_query(query, variables)
        game_player_count_records = []
        timestamp = int(time.time())

        for index, game in enumerate(games_cache):
            alias_name = f"game{index}"
            player_count = data[alias_name]
            popularity = data[f"{alias_name}_popularity"]

            game_status_cache[game] = {"playerCount": player_count,"popularity": popularity}
            game_player_count_records.append((timestamp, str(game), int(player_count)))

        await bot.add_game_player_records(game_player_count_records)

    except Exception as error:
        print(f"Failed to update game status cache")

def format_game_status(game: str, data: dict) -> str:
    game_name = GAME_DETAILS[game]["name"]
    emoji = bot.get_emoji(GAME_DETAILS[game]["emoji"])
    player_count = data["playerCount"]

    return f"{emoji} **{game_name}**\n Players: **{player_count}**\n Game Status: **{data['popularity']}**"

def build_status_message(game: str) -> str:
    status_parts = []
    if game == "all":
        for game in games_cache:
            data = game_status_cache.get(game)

            if data is not None:
                status_parts.append(format_game_status(game, data))

        return "\n\n".join(status_parts)
    
    data = game_status_cache.get(game)

    if data is None:
        return "No status data available"
    
    return format_game_status(game, data)

async def autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.lower()

    choices = []

    if not current or "all" in current:
        choices.append(app_commands.Choice(name="All", value="all"))

    filtered_games = [game for game in games_cache if current in game.lower()]

    choices.extend(app_commands.Choice(name=f"{GAME_DETAILS[game]['name']}", value=game) for game in filtered_games)
    return choices[:25]

@tasks.loop(seconds=10)
async def update_live_statuses():
    await update_game_status_cache()

    for message_id, info in list(live_status_messages.items()):
        message = info["message"]
        game = info["game"]
        try:
            if game == "all":
                status_parts = []
                for game in games_cache:
                    data = game_status_cache.get(game)
                    if data is not None:
                        status_parts.append(format_game_status(game, data))

                content = "\n\n".join(status_parts)

            else:
                data = game_status_cache.get(game)
                if data is None:
                    continue
                content = format_game_status(game, data)

            await message.edit(content=content)

        except discord.NotFound:
            print(f"Live status message {message_id} was deleted")
            await bot.delete_live_status_message(message_id)
            live_status_messages.pop(message_id, None)

        except discord.Forbidden:
            print(f"No permission to edit live status message {message_id}")

        except Exception as e:
            print(f"Live status update error: {e}")

@update_live_statuses.before_loop
async def before_update_live_statuses():
    await bot.wait_until_ready()

def is_valid_game(game: str) -> bool:
    return game == "all" or game in games_cache

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    message_id = payload.message_id

    if message_id in live_status_messages:
        live_status_messages.pop(message_id, None)
        await bot.delete_live_status_message(message_id)

@bot.tree.command(name="gamestatus", description="Get the player count and game status for a requested game")
@app_commands.describe(game="The game to check, or 'all' to include every game")
@app_commands.autocomplete(game=autocomplete)

async def get_game_status(interaction: discord.Interaction, game: str):
    try:
        await interaction.response.defer(ephemeral=True)

        if not game_status_cache:
            await update_game_status_cache()

        await interaction.followup.send(build_status_message(game))

    except Exception as error:
        print(f"Game status error: {error}")
        await interaction.followup.send("Failed to retrieve game status",ephemeral=True)

@bot.tree.command(name="livestatus", description="Get a permanent live updating player count and game status for a requested game")
@app_commands.describe(game="The game to monitor, or 'all' to include every game")
@app_commands.autocomplete(game=autocomplete)

async def get_live_status(interaction: discord.Interaction, game: str):
    if not is_valid_game(game):
        await interaction.response.send_message("Invalid game type", ephemeral=True)
        return

    message = None
    
    try:
        message = await interaction.channel.send(build_status_message(game))
        live_status_messages[message.id] = {"message": message, "game": game}
        try:
            await bot.add_live_status_message((message.guild.id, message.channel.id, message.id, game))

        except sqlite3.IntegrityError:
            live_status_messages.pop(message.id, None)

            try:
                await message.delete()
            except discord.HTTPException:
                pass

            await interaction.response.send_message("Server already has live status message", ephemeral=True)
            return

        await interaction.response.send_message("Live status created", ephemeral=True)

    except Exception as e:
        print(f"Live status error: {e}")

        if "message" in locals():
            try:
                await message.delete()
            except discord.HTTPException:
                pass


        await interaction.followup.send("Failed to create live status", ephemeral=True)

bot.run(DISCORD_TOKEN, log_handler=handler, log_level=logging.INFO)