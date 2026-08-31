from log import setupLogging
import discord
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
            if len(self.buffer) >= 3840:
                chunk = bytes(self.buffer[:3840])
                del self.buffer[:3840]
                return chunk

            elif self.is_finished:
                if len(self.buffer) > 0:
                    chunk = bytes(self.buffer)
                    chunk += b'\x00' * (3840 - len(chunk))
                    self.buffer.clear()
                    return chunk
                return b''

            else:
                return b'\x00' * 3840


class TTSHandling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = setupLogging("TTSHandling")

        # --- SHARED STATE ---
        self.last_speakers = {}
        self.user_profiles = {}
        self.guild_profiles = {}
        self.dm_mode_users = set()
        self.muted_users = []

        if not os.path.exists("./voices"):
            os.makedirs("./voices")

        # --- AUDIO QUEUE & CACHE ---
        self.tts_queue = asyncio.Queue()
        self.worker_task = None
        self.MAX_CACHED_VOICES = 10
        self.voice_cache = collections.OrderedDict()

        self.inflect_engine = inflect.engine()

    async def get_default_voice(self):
        voices = [f for f in os.listdir("./voices") if f.endswith(".onnx")]
        return voices[0] if voices else "default.onnx"

    def get_user_settings(self, user_id: int, default_voice: str):
        return self.user_profiles.get(user_id, {"speed": 1.0, "voice": default_voice})

    def get_guild_settings(self, guild_id: int):
        return self.guild_profiles.get(guild_id, {"tts_channel": -1})

    async def get_or_load_voice(self, model_path: str):
        if model_path in self.voice_cache:
            self.voice_cache.move_to_end(model_path)
            return self.voice_cache[model_path]

        def load_sync():
            return PiperVoice.load(model_path)

        try:
            self.logger.debug(f"Loading {model_path} into RAM...")
            voice = await asyncio.to_thread(load_sync)
        except Exception as e:
            self.logger.error(f"Failed to load Piper model {model_path}: {e}")
            return None

        self.voice_cache[model_path] = voice

        if len(self.voice_cache) > self.MAX_CACHED_VOICES:
            oldest_path, _ = self.voice_cache.popitem(last=False)
            self.logger.debug(f"Evicted {oldest_path} from RAM to save space.")

        return voice

    @commands.Cog.listener()
    async def on_typing(self, channel, user, when):
        if user.bot:
            return

        if user.id in self.muted_users: return

        is_valid = False
        if isinstance(channel, discord.DMChannel):
            if user.id in self.dm_mode_users:
                is_valid = True
        else:
            tts_channel_id = self.get_guild_settings(channel.guild.id)["tts_channel"]
            if tts_channel_id != -1 and channel.id != tts_channel_id:
                return

            if isinstance(user, discord.Member) and user.voice and user.voice.channel:
                vc = discord.utils.get(self.bot.voice_clients, guild=channel.guild)
                if vc and vc.is_connected() and vc.channel == user.voice.channel:
                    is_valid = True

        if not is_valid:
            return

        default_voice = await self.get_default_voice()
        settings = self.get_user_settings(user.id, default_voice)
        model_path = f"./voices/{settings['voice']}"

        if not os.path.exists(model_path):
            model_path = f"./voices/{default_voice}"

        if model_path not in self.voice_cache:
            self.logger.debug(f"[Pre-load] {user.display_name} started typing. Warming up {model_path}...")
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

        if '"' in text:
            parts = text.split('"')
            new_text = ""
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    new_text += part
                else:
                    new_text += part + (" quote unquote " if i % 2 == 0 else ", ")
            text = new_text

        target_symbols = '#$%&^*~{}[]|<>/\\'
        parsed_chars = []
        for char in text:
            if char in target_symbols:
                try:
                    symbol_name = unicodedata.name(char).lower()
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

        def replace_numbers(match):
            try:
                words = self.inflect_engine.number_to_words(match.group(0))
                return f" {words} "
            except Exception:
                return match.group(0)

        text = re.sub(r'\b\d+\b', replace_numbers, text)

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
        try:
            length_scale = 1.0 / speed_multiplier

            if hasattr(voice, "config"):
                if isinstance(voice.config, dict):
                    if "inference" not in voice.config:
                        voice.config["inference"] = {}
                    voice.config["inference"]["length_scale"] = length_scale
                else:
                    voice.config.length_scale = length_scale

            audio_state = None

            for audio_frame in voice.synthesize(text):
                raw_pcm = audio_frame.audio_int16_bytes
                if not raw_pcm:
                    continue

                # --- NEW: AUDIO NORMALIZATION ---
                # 1. Measure the current loudness of this specific sentence
                # The '2' means it's 16-bit (2-byte) audio
                current_loudness = audioop.rms(raw_pcm, 2)

                # 2. Define our target loudness (you may need to tweak this number, ~4000-6000 is usually good for Discord)
                TARGET_LOUDNESS = 4000

                if 0 < current_loudness < TARGET_LOUDNESS:
                    # Calculate how much we need to multiply the volume to reach the target
                    volume_multiplier = TARGET_LOUDNESS / current_loudness

                    # Cap the multiplier at 3.0x to prevent destroying the speakers on a sudden loud noise (like a breath or click)
                    volume_multiplier = min(volume_multiplier, 3.0)

                    # Multiply the raw PCM bytes by the factor
                    raw_pcm = audioop.mul(raw_pcm, 2, volume_multiplier)
                # ---------------------------------

                # Resample and convert to Stereo
                native_rate = voice.config.sample_rate if hasattr(voice, "config") else 22050
                resampled, audio_state = audioop.ratecv(raw_pcm, 2, 1, native_rate, 48000, audio_state)
                stereo = audioop.tostereo(resampled, 2, 1, 1)

                # Push safely to the stream
                audio_source.add_data(stereo)

        except Exception as e:
            self.logger.error(f"Piper Generation Error: {e}")
        finally:
            audio_source.is_finished = True

    async def tts_worker(self):
        while True:
            try:
                voice_client, text, speed_multiplier, voice_file = await self.tts_queue.get()

                while voice_client.is_connected() and voice_client.is_playing():
                    await asyncio.sleep(0.05)

                if not voice_client.is_connected():
                    self.tts_queue.task_done()
                    continue

                model_path = f"./voices/{voice_file}"
                if not os.path.exists(model_path):
                    model_path = f"./voices/{await self.get_default_voice()}"

                voice = await self.get_or_load_voice(model_path)
                if voice is None:
                    self.tts_queue.task_done()
                    continue

                audio_source = StreamingAudioSource()
                audio_source.add_data(b'\x00' * (3840 * 3))

                voice_client.play(audio_source,
                                  after=lambda e: self.logger.error(f"Playback Error: {e}") if e else None)

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
    await bot.add_cog(TTSHandling(bot))