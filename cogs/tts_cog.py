from log import setupLogging
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
import re
import emoji
import threading
import audioop
import collections
import unicodedata
import inflect

# --- STEP 3: ONNX Thread Tuning ---
# These MUST be set before Piper is imported!
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["ONNXRUNTIME_INTRA_OP_NUM_THREADS"] = "1"
os.environ["ONNXRUNTIME_INTER_OP_NUM_THREADS"] = "1"

from piper import PiperVoice

class StreamingAudioSource(discord.AudioSource):
    """
    A custom Discord AudioSource that acts as an in-memory stream buffer.
    It takes raw 48kHz stereo PCM bytes and feeds them to Discord 20ms at a time.
    """
    def __init__(self):
        self.buffer = bytearray()
        self.is_finished = False
        self.lock = threading.Lock()
        
    def add_data(self, data: bytes):
        with self.lock:
            self.buffer.extend(data)
            
    def read(self) -> bytes:
        with self.lock:
            # Discord requires exactly 3840 bytes (20ms of audio) per tick
            if len(self.buffer) >= 3840:
                chunk = bytes(self.buffer[:3840])
                del self.buffer[:3840]
                return chunk
                
            elif self.is_finished:
                # If Piper is done but we have leftover bytes, pad with silence to equal 3840
                if len(self.buffer) > 0:
                    chunk = bytes(self.buffer)
                    chunk += b'\x00' * (3840 - len(chunk))
                    self.buffer.clear()
                    return chunk
                return b'' # Empty byte string tells Discord to stop playing
                
            else:
                # Piper is still calculating the next chunk, so we feed Discord silence to keep the connection alive
                return b'\x00' * 3840

class TTS(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = setupLogging("TTSCog")
        self.last_speakers = {}
        self.user_profiles = {}
        self.guild_profiles = {}
        self.dm_mode_users = set()
        
        if not os.path.exists("./voices"):
            os.makedirs("./voices")

        # Unified Queue for the streaming architecture
        self.tts_queue = asyncio.Queue()
        self.worker_task = None

        self.muted_users = []

        self.MAX_CACHED_VOICES = 10
        self.voice_cache = collections.OrderedDict()

        # Initialize the number-to-words engine once
        self.inflect_engine = inflect.engine()

    async def get_default_voice(self):
        voices = [f for f in os.listdir("./voices") if f.endswith(".onnx")]
        return voices[0] if voices else "default.onnx"

    def get_user_settings(self, user_id: int, default_voice: str):
        return self.user_profiles.get(user_id, {"speed": 1.0, "voice": default_voice})

    def get_guild_settings(self, guild_id: int):
        return self.guild_profiles.get(guild_id, {"tts_channel": -1})

    async def get_or_load_voice(self, model_path: str):
        """Fetches a warm PiperVoice from RAM, or loads it safely if it isn't cached."""
        if model_path in self.voice_cache:
            # Move to the end to mark it as the most recently used
            self.voice_cache.move_to_end(model_path)
            return self.voice_cache[model_path]

        # If it's not in the cache, load it using a background thread so we don't freeze Discord
        def load_sync():
            return PiperVoice.load(model_path)

        try:
            self.logger.debug(f"Loading {model_path} into RAM...")
            voice = await asyncio.to_thread(load_sync)
        except Exception as e:
            self.logger.error(f"Failed to load Piper model {model_path}: {e}")
            return None

        # Add the newly loaded voice to the cache
        self.voice_cache[model_path] = voice

        # If we exceeded our RAM limit, evict the oldest (Least Recently Used) model
        if len(self.voice_cache) > self.MAX_CACHED_VOICES:
            oldest_path, _ = self.voice_cache.popitem(last=False)
            self.logger.debug(f"Evicted {oldest_path} from RAM to save space.")

        return voice

    @commands.command(name="mute", description="Mute a user from using the bot, OWNER ONLY")
    @commands.is_owner()
    async def mute_user(self, ctx: commands.Context, member: discord.Member):
        if member.id not in self.muted_users:
            self.muted_users.append(member.id)
            await ctx.reply("I have muted `" + member.name + "` from using the bot.")
        else:
            self.muted_users.remove(member.id)
            await ctx.reply("I have unmuted `" + member.name + "` from using the bot.")

    @commands.command(name="poll_muted", description="sends what users are muted")
    async def poll_muted(self, ctx: commands.Context):
        message = "Here is the list of muted users: \n"

        for mem in self.muted_users:
            message += f"\t<@{mem}>"

        await ctx.reply(message)

    @app_commands.command(name="dm_mode", description="Toggle DM Mode to send TTS messages privately via DMs.")
    async def dm_mode(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if user_id in self.dm_mode_users:
            self.dm_mode_users.remove(user_id)
            await interaction.response.send_message("DM Mode **disabled**. You can no longer send me DMs to speak.", ephemeral=True)
        else:
            self.dm_mode_users.add(user_id)
            await interaction.response.send_message("DM Mode **enabled**! You can now send me a Direct Message, and I will read it out loud in your current voice channel.", ephemeral=True)

    @app_commands.command(name="join", description="Joins your current voice channel.")
    async def join(self, interaction: discord.Interaction):
        if interaction.user.voice:
            channel = interaction.user.voice.channel
            voice_client = discord.utils.get(self.bot.voice_clients, guild=interaction.guild)
            if voice_client is None:
                await channel.connect()
                self.last_speakers[interaction.guild.id] = None
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
        if multiplier < 0.1 or multiplier > 5.0:
            await interaction.response.send_message("Please choose a speed between 0.1 and 5.0.", ephemeral=True)
            return

        user_id = interaction.user.id
        default_voice = await self.get_default_voice()

        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = {"speed": 1.0, "voice": default_voice}
        self.user_profiles[user_id]["speed"] = multiplier

        await self.bot.db.execute("""
            INSERT INTO users (user_id, speed, voice)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET speed=excluded.speed
        """, (user_id, multiplier, self.user_profiles[user_id]["voice"]))
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
    @app_commands.describe(voice_file="Select a voice from the list")
    async def voice(self, interaction: discord.Interaction, voice_file: str):
        if not os.path.exists(f"./voices/{voice_file}"):
            await interaction.response.send_message("That voice model does not exist.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id not in self.user_profiles:
            self.user_profiles[user_id] = {"speed": 1.0, "voice": voice_file}
        else:
            self.user_profiles[user_id]["voice"] = voice_file

        await self.bot.db.execute("""
            INSERT INTO users (user_id, speed, voice)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET voice=excluded.voice
        """, (user_id, self.user_profiles[user_id]["speed"], voice_file))
        await self.bot.db.commit()
        await interaction.response.send_message(f"Your TTS voice has been updated to **{voice_file.replace('.onnx', '')}**.")

    @app_commands.command(name="voices", description="Lists all available TTS voices.")
    async def voices_list(self, interaction: discord.Interaction):
        voices = [f.replace(".onnx", "") for f in os.listdir("./voices") if f.endswith(".onnx")]
        if not voices:
            await interaction.response.send_message("No custom voices are currently installed.", ephemeral=True)
            return
        voice_str = "\n".join([f"• {v}" for v in voices])
        await interaction.response.send_message(f"**Available Voices:**\n{voice_str}")

    @app_commands.command(name="set_tts_channel", description="Set a certain channel as TTS.")
    @commands.has_permissions(manage_channels=True)
    async def set_tts_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        guild_id = interaction.guild_id
        tts_channel = channel.id

        if guild_id not in self.guild_profiles:
            self.guild_profiles[guild_id] = {"tts_channel": tts_channel}
        else:
            self.guild_profiles[guild_id]["tts_channel"] = tts_channel

        await self.bot.db.execute("""
                                  INSERT INTO guilds (guild_id, tts_channel)
                                  VALUES (?, ?)
                                  ON CONFLICT(guild_id) DO UPDATE SET tts_channel=excluded.tts_channel
                                  """, (guild_id, tts_channel))

        await self.bot.db.commit()
        await interaction.response.send_message(f"Updated guild TTS channel to **{tts_channel}**.")

    @app_commands.command(name="clear_tts_channel",
                          description="Removes the TTS channel restriction, allowing the bot to read from all channels.")
    @commands.has_permissions(manage_channels=True)
    async def clear_tts_channel(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id

        # 1. Update the in-memory dictionary cache back to -1
        if guild_id not in self.guild_profiles:
            self.guild_profiles[guild_id] = {"tts_channel": -1}
        else:
            self.guild_profiles[guild_id]["tts_channel"] = -1

        # 2. Update the SQLite database
        await self.bot.db.execute("""
                                  INSERT INTO guilds (guild_id, tts_channel)
                                  VALUES (?, ?)
                                  ON CONFLICT(guild_id) DO UPDATE SET tts_channel=excluded.tts_channel
                                  """, (guild_id, -1))

        await self.bot.db.commit()
        await interaction.response.send_message(
            "Removed the TTS channel restriction. The bot will now read messages from any channel again.")

    @commands.Cog.listener()
    async def on_typing(self, channel, user, when):
        if user.bot:
            return

        if user.id in self.muted_users: return

        # 1. Quick check to ensure we only load if they are valid to speak
        is_valid = False
        if isinstance(channel, discord.DMChannel):
            if user.id in self.dm_mode_users:
                is_valid = True
        else:
            # Check if this channel is the designated TTS channel (if one is set)
            tts_channel_id = self.get_guild_settings(channel.guild.id)["tts_channel"]
            if tts_channel_id != -1 and channel.id != tts_channel_id:
                return

            # If in a server, ensure they are in a VC and the bot is in that same VC
            if isinstance(user, discord.Member) and user.voice and user.voice.channel:
                vc = discord.utils.get(self.bot.voice_clients, guild=channel.guild)
                if vc and vc.is_connected() and vc.channel == user.voice.channel:
                    is_valid = True

        if not is_valid:
            return

        # 2. Get their requested voice profile
        default_voice = await self.get_default_voice()
        settings = self.get_user_settings(user.id, default_voice)
        model_path = f"./voices/{settings['voice']}"

        if not os.path.exists(model_path):
            model_path = f"./voices/{default_voice}"

        # 3. If the voice is NOT in RAM, fire off a background task to load it instantly
        if model_path not in self.voice_cache:
            self.logger.debug(f"[Pre-load] {user.display_name} started typing. Warming up {model_path}...")
            # We use create_task so we don't freeze the event loop waiting for it to load
            self.bot.loop.create_task(self.get_or_load_voice(model_path))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        voice_client = None
        guild_id = None
        display_name = message.author.display_name

        if message.author.id in self.muted_users: return

        if message.guild is None:
            if message.author.id not in self.dm_mode_users:
                return
            for vc in self.bot.voice_clients:
                for member in vc.channel.members:
                    if member.id == message.author.id:
                        voice_client = vc
                        guild_id = vc.guild.id
                        display_name = member.display_name
                        break
                if voice_client:
                    break
            if not voice_client:
                await message.channel.send("I couldn't find you in any voice channels I'm currently connected to!")
                return
        else:
            tts_channel_id = self.get_guild_settings(message.guild.id)["tts_channel"]

            # If a channel is set (not -1), and this message ISN'T in that channel, ignore it.
            if tts_channel_id != -1 and message.channel.id != tts_channel_id:
                return

            voice_client = discord.utils.get(self.bot.voice_clients, guild=message.guild)
            if not voice_client or not voice_client.is_connected():
                return
            if not message.author.voice or message.author.voice.channel != voice_client.channel:
                return
            guild_id = message.guild.id
            display_name = message.author.display_name

        text = message.clean_content
        text = re.sub(r'(https?://\S+)', 'a link', text)
        if message.attachments:
            text = f"{text} a file attachment".strip()

        def custom_emoji_repl(match):
            name = match.group(1).replace('_', ' ')
            return f" {name} emoji "
        text = re.sub(r'<a?:([a-zA-Z0-9_]+):[0-9]+>', custom_emoji_repl, text)
        text = emoji.demojize(text)

        def unicode_emoji_repl(match):
            name = match.group(1).replace('_', ' ').replace('-', ' ')
            return f" {name} emoji "
        text = re.sub(r':([a-zA-Z0-9_\-]+):', unicode_emoji_repl, text)
        text = re.sub(r'(^|\s)\?+(?=\s|$)', r'\1 question mark ', text)

        # 6. Quotations ("quote, unquote")
        if '"' in text:
            parts = text.split('"')
            new_text = ""
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    new_text += part
                else:
                    new_text += part + (" quote, " if i % 2 == 0 else ", unquote ")
            text = new_text

        # 7. Unicodedata Symbol Parsing
        # Explicitly targets symbols while preserving standard punctuation (. , ! ?) for prosody
        target_symbols = '#$%&^*~{}[]|<>/\\'
        parsed_chars = []
        for char in text:
            if char in target_symbols:
                try:
                    symbol_name = unicodedata.name(char).lower()
                    self.logger.info("data + " + symbol_name)
                    if symbol_name == "number sign":
                        symbol_name = "hashtag"
                    elif symbol_name == "ampersand":
                        symbol_name = "and"
                    elif symbol_name == "solidus":
                        symbol_name = "slash"
                    elif symbol_name == "reverse solidus":
                        symbol_name = "backslash"
                    parsed_chars.append(f" {symbol_name} ")
                except ValueError:
                    parsed_chars.append(char)
            else:
                parsed_chars.append(char)
        text = "".join(parsed_chars)

        # 8. Inflect Number Parsing
        # Finds standalone numbers and converts them (e.g. "123" -> "one hundred and twenty-three")
        def replace_numbers(match):
            try:
                words = self.inflect_engine.number_to_words(match.group(0))
                return f" {words} "
            except Exception:
                return match.group(0)

        text = re.sub(r'\b\d+\b', replace_numbers, text)

        # 9. Aggressive Sanitization
        # Now that everything is translated to words, we strip out any remaining unreadable junk
        text = re.sub(r'[^\w\s\.,!\?\'"-]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

        if not text:
            return

        max_length = 300
        if len(text) > max_length:
            text = text[:max_length] + "... and the rest was too long."

        last_speaker = self.last_speakers.get(guild_id)
        if last_speaker != message.author.id:
            intro = f"{display_name} said: "
            self.last_speakers[guild_id] = message.author.id
        else:
            intro = ""

        final_text = f"{intro}{text}"
        default_voice = await self.get_default_voice()
        settings = self.get_user_settings(message.author.id, default_voice)

        await self.tts_queue.put((voice_client, final_text, settings["speed"], settings["voice"]))

    def generate_and_feed_sync(self, audio_source, voice, text, speed_multiplier):
        """Synchronous TTS generation logic passed safely to a background thread."""
        try:
            length_scale = 1.0 / speed_multiplier

            # Set speed configuration safely for 1.6.0+
            if hasattr(voice, "config"):
                if isinstance(voice.config, dict):
                    if "inference" not in voice.config:
                        voice.config["inference"] = {}
                    voice.config["inference"]["length_scale"] = length_scale
                else:
                    voice.config.length_scale = length_scale

            audio_state = None

            # Stream frames directly
            for audio_frame in voice.synthesize(text):
                raw_pcm = audio_frame.audio_int16_bytes
                if not raw_pcm:
                    continue

                # Resample and convert to Stereo
                native_rate = voice.config.sample_rate if hasattr(voice, "config") else 22050
                resampled, audio_state = audioop.ratecv(raw_pcm, 2, 1, native_rate, 48000, audio_state)
                stereo = audioop.tostereo(resampled, 2, 1, 1)

                # Push safely to the stream
                audio_source.add_data(stereo)

        except Exception as e:
            self.logger.error(f"Piper Generation Error: {e}")
        finally:
            # Always close the stream gracefully so Discord doesn't hang
            audio_source.is_finished = True

    async def tts_worker(self):
        """Unified Pipeline: Instantly hooks Piper output directly into Discord."""
        while True:
            try:
                voice_client, text, speed_multiplier, voice_file = await self.tts_queue.get()

                # Wait for the Discord mic to be completely free
                while voice_client.is_connected() and voice_client.is_playing():
                    await asyncio.sleep(0.05)

                if not voice_client.is_connected():
                    self.tts_queue.task_done()
                    continue

                model_path = f"./voices/{voice_file}"
                if not os.path.exists(model_path):
                    model_path = f"./voices/{await self.get_default_voice()}"

                # STEP 2: Use the LRU Cache!
                # Fetches the warm model from RAM or loads it safely in the background
                voice = await self.get_or_load_voice(model_path)
                if voice is None:
                    self.tts_queue.task_done()
                    continue

                # Prepare the stream and hook it into Discord immediately
                audio_source = StreamingAudioSource()

                # --- STEP 3: Silence Padding ---
                # Push exactly 60ms (3 Discord ticks) of pure silence into the buffer first.
                # This wakes up Discord's Opus encoder so the first syllable isn't clipped.
                audio_source.add_data(b'\x00' * (3840 * 3))

                voice_client.play(audio_source,
                                  after=lambda e: self.logger.error(f"Playback Error: {e}") if e else None)

                # Spin up the generator safely, passing the variables explicitly so they don't get lost in closures
                await asyncio.to_thread(self.generate_and_feed_sync, audio_source, voice, text, speed_multiplier)

                self.tts_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in TTS worker: {e}")

    async def cog_load(self):
        self.logger.debug(f"{self.__class__.__name__} loaded!")
        async with self.bot.db.execute("SELECT user_id, speed, voice FROM users") as cursor:
            async for row in cursor:
                self.user_profiles[row[0]] = {"speed": row[1], "voice": row[2]}

        async with self.bot.db.execute("SELECT guild_id, tts_channel FROM guilds") as cursor:
            async for row in cursor:
                self.guild_profiles[row[0]] = {"tts_channel": row[1]}
                
        self.logger.debug(f"Loaded {len(self.user_profiles)} user profiles into memory.")
        self.worker_task = self.bot.loop.create_task(self.tts_worker())

    async def cog_unload(self):
        self.logger.debug(f"{self.__class__.__name__} unloaded!")
        if self.worker_task:
            self.worker_task.cancel()

async def setup(bot):
    await bot.add_cog(TTS(bot))
