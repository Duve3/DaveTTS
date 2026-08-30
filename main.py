import logging
from log import setupLogging
import discord
import os
from discord.ext import commands
import aiosqlite
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# globals
cogPath = "cogs."
# Read the DEBUG flag from .env, default to False if not present
debug = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
TOKEN = os.getenv("DISCORD_TOKEN")

def getCogs():
    cogList = []
    for file in os.listdir(cogPath.replace(".", "/")):
        if file.endswith(".py") and not file.startswith("DISABLED_") and file != "log.py":
            cogList.append(file.split(".")[0])
    return cogList


class Client(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        prefix = "$$!!" if debug else "!"

        super().__init__(
            command_prefix=prefix,
            intents=intents
        )
        self.db = None

    async def setup_hook(self):
        # 1. Initialize the SQLite Database
        self.db = await aiosqlite.connect("ttsbot.db")

        # 2. Create the users table if it doesn't exist
        await self.db.execute("""
                              CREATE TABLE IF NOT EXISTS users
                              (
                                  user_id
                                      INTEGER
                                      PRIMARY
                                          KEY,
                                  speed
                                      REAL
                                      DEFAULT
                                          1.0,
                                  voice
                                      TEXT
                              )
                              """)

        # 3. Create the guilds table if it doesn't exist
        await self.db.execute("""
                              CREATE TABLE IF NOT EXISTS guilds
                              (
                                guild_id
                                    INTEGER
                                    PRIMARY
                                        KEY,
                                tts_channel
                                    INTEGER
                                    DEFAULT
                                        -1
                              )      
                              """)

        await self.db.commit()

        # 4. Load Cogs
        for cog in getCogs():
            await self.load_extension(f"{cogPath}{cog}")

    async def close(self):
        # Safely close the database connection when the bot shuts down
        if self.db:
            await self.db.close()
        await super().close()


client = Client()


@client.command(name="reloadCogs")
async def reloadCogs(ctx):
    # Kept as a prefix command since it's an admin/owner tool
    if ctx.author.id == 680116696819957810:
        logger.debug("Reloading all cogs!")
        for cog in getCogs():
            await client.reload_extension(f"{cogPath}{cog}")
        # Re-sync slash commands in case you added new ones
        await client.tree.sync()
        await ctx.reply("Reloaded all Cogs and synced slash commands!")
    else:
        await ctx.reply(f"{ctx.author.mention} :gun:")


@client.event
async def on_ready():
    logger.info(
        f"I have successfully logged in as:\n\t{client.user.name}#{client.user.discriminator}\n\tID: {client.user.id}")


def main():
    if not TOKEN:
        logger.error("Failed to find DISCORD_TOKEN inside the .env file! Exiting...")
        return
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    logger = setupLogging("main", level=logging.DEBUG)
    setupLogging("discord", level=logging.INFO)
    setupLogging("discord.http", level=logging.INFO)
    try:
        main()
    finally:
        pass