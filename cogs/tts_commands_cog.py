from log import setupLogging
import discord
from discord import app_commands
from discord.ext import commands
import os


class TTSCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = setupLogging("TTSCommands")

    @property
    def handler(self):
        """Helper to fetch the master state from the TTSHandling cog."""
        return self.bot.get_cog("TTSHandling")

    @commands.command(name="mute", description="Mute a user from using the bot, OWNER ONLY")
    @commands.is_owner()
    async def mute_user(self, ctx: commands.Context, member: discord.Member):
        if not self.handler: return

        if member.id not in self.handler.muted_users:
            self.handler.muted_users.append(member.id)
            await ctx.reply("I have muted `" + member.name + "` from using the bot.")
        else:
            self.handler.muted_users.remove(member.id)
            await ctx.reply("I have unmuted `" + member.name + "` from using the bot.")

    @commands.command(name="poll_muted", description="sends what users are muted")
    async def poll_muted(self, ctx: commands.Context):
        if not self.handler: return

        message = "Here is the list of muted users: \n"
        for mem in self.handler.muted_users:
            message += f"\t<@{mem}>"

        await ctx.reply(message)

    @app_commands.command(name="dm_mode", description="Toggle DM Mode to send TTS messages privately via DMs.")
    async def dm_mode(self, interaction: discord.Interaction):
        if not self.handler: return

        user_id = interaction.user.id
        if user_id in self.handler.dm_mode_users:
            self.handler.dm_mode_users.remove(user_id)
            await interaction.response.send_message("DM Mode **disabled**. You can no longer send me DMs to speak.",
                                                    ephemeral=True)
        else:
            self.handler.dm_mode_users.add(user_id)
            await interaction.response.send_message(
                "DM Mode **enabled**! You can now send me a Direct Message, and I will read it out loud in your current voice channel.",
                ephemeral=True)

    @app_commands.command(name="join", description="Joins your current voice channel.")
    async def join(self, interaction: discord.Interaction):
        if not self.handler: return

        if interaction.user.voice:
            channel = interaction.user.voice.channel
            voice_client = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)
            if voice_client is None:
                await channel.connect()
                self.handler.last_speakers[interaction.guild.id] = None
                await interaction.response.send_message(f"Joined **{channel.name}**! Type in chat to hear TTS.")
            else:
                await voice_client.move_to(channel)
                await interaction.response.send_message(f"Moved to **{channel.name}**.")
        else:
            await interaction.response.send_message("You need to join a voice channel first!", ephemeral=True)

    @app_commands.command(name="leave", description="Leaves the current voice channel.")
    async def leave(self, interaction: discord.Interaction):
        voice_client = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)
        if voice_client:
            await voice_client.disconnect()
            await interaction.response.send_message("Left the voice channel.")
        else:
            await interaction.response.send_message("I'm not currently in a voice channel.", ephemeral=True)

    @app_commands.command(name="speed", description="Changes your TTS speaking speed (0.1x to 5.0x)")
    @app_commands.describe(multiplier="The speed multiplier (e.g. 1.0 is normal, 2.0 is double speed)")
    async def speed(self, interaction: discord.Interaction, multiplier: float):
        if not self.handler:
            await interaction.response.send_message("TTS System offline.", ephemeral=True)
            return

        if multiplier < 0.1 or multiplier > 5.0:
            await interaction.response.send_message("Please choose a speed between 0.1 and 5.0.", ephemeral=True)
            return

        user_id = interaction.user.id
        default_voice = await self.handler.get_default_voice()

        if user_id not in self.handler.user_profiles:
            self.handler.user_profiles[user_id] = {"speed": 1.0, "voice": default_voice}
        self.handler.user_profiles[user_id]["speed"] = multiplier

        await self.bot.db.execute("""
                                  INSERT INTO users (user_id, speed, voice)
                                  VALUES (?, ?, ?)
                                  ON CONFLICT(user_id) DO UPDATE SET speed=excluded.speed
                                  """, (user_id, multiplier, self.handler.user_profiles[user_id]["voice"]))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Your TTS speed has been set to **{multiplier}x**.")

    async def voice_autocomplete(self, interaction: discord.Interaction, current: str):
        voices = [f for f in os.listdir("./voices") if f.endswith(".onnx")]
        return [
            app_commands.Choice(name=voice.replace(".onnx", ""), value=voice)
            for voice in voices if current.lower() in voice.lower()
        ][:25]

    @app_commands.command(name="voice", description="Select your personal TTS voice.")
    @app_commands.autocomplete(voice_file=voice_autocomplete)
    @app_commands.describe(voice_file="Pick a voice from the list")
    async def voice(self, interaction: discord.Interaction, voice_file: str):
        if not self.handler: return

        if not os.path.exists(f"./voices/{voice_file}"):
            await interaction.response.send_message("That voice model does not exist.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id not in self.handler.user_profiles:
            self.handler.user_profiles[user_id] = {"speed": 1.0, "voice": voice_file}
        else:
            self.handler.user_profiles[user_id]["voice"] = voice_file

        await self.bot.db.execute("""
                                  INSERT INTO users (user_id, speed, voice)
                                  VALUES (?, ?, ?)
                                  ON CONFLICT(user_id) DO UPDATE SET voice=excluded.voice
                                  """, (user_id, self.handler.user_profiles[user_id]["speed"], voice_file))
        await self.bot.db.commit()
        await interaction.response.send_message(
            f"Your TTS voice has been updated to **{voice_file.replace('.onnx', '')}**.")

    @app_commands.command(name="voices", description="Lists all available TTS voices.")
    async def voices_list(self, interaction: discord.Interaction):
        if not os.path.exists("./voices"):
            return

        voices = [f.replace(".onnx", "") for f in os.listdir("./voices") if f.endswith(".onnx")]
        if not voices:
            await interaction.response.send_message("No custom voices are currently installed.", ephemeral=True)
            return
        voice_str = "\n".join([f"• {v}" for v in voices])
        await interaction.response.send_message(f"**Available Voices:**\n{voice_str}")

    @app_commands.command(name="set_tts_channel", description="Set a certain channel as TTS.")
    @commands.has_permissions(manage_channels=True)
    async def set_tts_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not self.handler: return

        guild_id = interaction.guild_id
        tts_channel = channel.id

        if guild_id not in self.handler.guild_profiles:
            self.handler.guild_profiles[guild_id] = {"tts_channel": tts_channel}
        else:
            self.handler.guild_profiles[guild_id]["tts_channel"] = tts_channel

        await self.bot.db.execute("""
                                  INSERT INTO guilds (guild_id, tts_channel)
                                  VALUES (?, ?)
                                  ON CONFLICT(guild_id) DO UPDATE SET tts_channel=excluded.tts_channel
                                  """, (guild_id, tts_channel))

        await self.bot.db.commit()
        await interaction.response.send_message(f"Updated guild TTS channel to **<#{tts_channel}>**.")

    @app_commands.command(name="clear_tts_channel", description="Removes the TTS channel restriction.")
    @commands.has_permissions(manage_channels=True)
    async def clear_tts_channel(self, interaction: discord.Interaction):
        if not self.handler: return

        guild_id = interaction.guild_id

        if guild_id not in self.handler.guild_profiles:
            self.handler.guild_profiles[guild_id] = {"tts_channel": -1}
        else:
            self.handler.guild_profiles[guild_id]["tts_channel"] = -1

        await self.bot.db.execute("""
                                  INSERT INTO guilds (guild_id, tts_channel)
                                  VALUES (?, ?)
                                  ON CONFLICT(guild_id) DO UPDATE SET tts_channel=excluded.tts_channel
                                  """, (guild_id, -1))

        await self.bot.db.commit()
        await interaction.response.send_message(
            "Removed the TTS channel restriction. The bot will now read messages from any channel again.")


async def setup(bot):
    await bot.add_cog(TTSCommands(bot))