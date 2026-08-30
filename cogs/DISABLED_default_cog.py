from discord.ext import commands
import discord
from log import setupLogging


class Cog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = setupLogging(f"{self.__class__.__name__}")

    @commands.command(name="example")
    async def example(self):
        self.logger.info("exmaple")

    # doing something when the cog gets loaded
    async def cog_load(self):
        self.logger.debug(f"{self.__class__.__name__} loaded!")

    # doing something when the cog gets unloaded
    async def cog_unload(self):
        self.logger.debug(f"{self.__class__.__name__} unloaded!")


# usually you’d use cogs in extensions
# you would then define a global async function named 'setup', and it would take 'bot' as its only parameter
async def setup(bot):
    # finally, adding the cog to the bot
    await bot.add_cog(Cog(bot=bot))
