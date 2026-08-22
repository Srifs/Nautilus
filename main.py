import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from dotenv import load_dotenv
import os
import aiohttp
from logging.handlers import RotatingFileHandler


load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MCC_API_KEY = os.getenv("MCC_API_KEY")
MCC_API_URL = "https://api.mccisland.net/graphql"

handler = RotatingFileHandler("discord.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)

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

    async def close(self):
        await self.http_session.close()
        await super().close()

bot = Bot(command_prefix="/", intents=intents)

@bot.event
async def on_ready():
    print(f"{bot.user} is up")

    try:
        await update_games()

        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")

        if not update_live_statuses.is_running():
            update_live_statuses.start()

    except Exception as e:
        print(f"Failed to sync commands: {e}")

async def update_games():
    global games_cache, games_cache_time
    query = """query availableQueueTypes { availableQueueTypes }"""

    try:
        data = await mcci_query(query)
        games_cache = data["availableQueueTypes"]
        print(f"Loaded game types: {games_cache}")

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

        for index, game in enumerate(games_cache):
            alias_name = f"game{index}"
            game_status_cache[game] = {
                "playerCount": data[alias_name],
                "popularity": data[f"{alias_name}_popularity"]
            }

    except Exception as error:
        print(f"Failed to update game status cache: {error}")

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
        return "No status data available."
    
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
            print(f"Live status message {message_id} was deleted.")
            del live_status_messages[message_id]

        except discord.Forbidden:
            print(f"No permission to edit live status message {message_id}.")
            del live_status_messages[message_id]

        except Exception as e:
            print(f"Live status update error: {e}")

@update_live_statuses.before_loop
async def before_update_live_statuses():
    await bot.wait_until_ready()

def is_valid_game(game: str) -> bool:
    return game == "all" or game in games_cache

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
        await interaction.followup.send("Failed to retrieve game status.",ephemeral=True)

@bot.tree.command(name="livestatus", description="Get a permanent live updating player count and game status for a requested game")
@app_commands.describe(game="The game to monitor, or 'all' to include every game")
@app_commands.autocomplete(game=autocomplete)

async def get_live_status(interaction: discord.Interaction, game: str):
    if not is_valid_game(game):
        await interaction.response.send_message("Invalid game type.", ephemeral=True)
        return
    
    try:
        message = await interaction.channel.send(build_status_message(game))
        live_status_messages[message.id] = {"message": message, "game": game}

        await interaction.response.send_message("Live status created", ephemeral=True)
    except Exception as e:
        print(f"Live status error: {e}")
        await interaction.followup.send("Failed to create live status.", ephemeral=True)

bot.run(DISCORD_TOKEN, log_handler=handler, log_level=logging.INFO)