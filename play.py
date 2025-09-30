import asyncio
import ctypes
import json
import os
import random
import tempfile
from pathlib import Path
from time import sleep

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from yt_dlp import YoutubeDL

import logging


# Object for managing playlists at the bot level (name -> list of songs)
playlist_data = {}

def get_secret_value(key: str, default=None) -> str:
    """
    Get secret value from Docker secret or environment variable. Falls back to default if not found.
    """
    secret_path = f'/run/secrets/{key}'
    if os.path.exists(secret_path):
        with open(secret_path) as f_in:
            return f_in.read().strip()
    return os.environ.get(key, default)

INTENTS = discord.Intents.default()
BOT = commands.Bot(command_prefix="!", intents=INTENTS)

def ensure_opus_loaded():
    """Ensure Opus library is loaded for voice support. This is problematic in some Docker images."""
    if discord.opus.is_loaded():
        logger.debug("Opus already loaded")
        return

    candidates = [
        ctypes.util.find_library("opus"),  # resolves to full path if available
        "libopus.so.0",                    # Debian/Ubuntu + Alpine runtime soname
        "libopus.so",                      # dev symlink (may not exist in runtime-only images)
    ]

    last_err = None
    for name in candidates:
        if not name:
            continue
        try:
            discord.opus.load_opus(name)
            logger.info(f"Loaded Opus from: {name}")
            break
        except Exception as e:
            last_err = e
            logger.debug(f"Failed to load Opus from {name}: {e!r}")

    if not discord.opus.is_loaded():
        logger.error("Could not load libopus: {last_err!r}")

def ensure_vc_for(interaction: discord.Interaction) -> discord.VoiceClient | None:
    """
    Get the current voice client for this guild or None.
    """
    if interaction.guild is None:
        return None
    return discord.utils.get(BOT.voice_clients, guild=interaction.guild)


async def ensure_join_same_channel(interaction: discord.Interaction) -> discord.VoiceClient:
    """
    Join the user's voice channel if not already connected.
    """
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        raise commands.CommandError('Käyttäjää ei tunnistettu.')

    if interaction.user.voice is None or interaction.user.voice.channel is None:
        raise commands.CommandError('Liity ensin äänikanavalle.')

    vc = ensure_vc_for(interaction)
    if vc and vc.channel.id == interaction.user.voice.channel.id:
        return vc

    # If bot is already in a different channel, move
    if vc:
        await vc.move_to(interaction.user.voice.channel)
        return vc

    # Not connected, so connect
    return await interaction.user.voice.channel.connect(reconnect=True)

def _after_factory(bot: commands.Bot, guild_id: int, vc: discord.VoiceClient):
    """Create an 'after' callback that safely jumps back to the event loop."""
    def _after_playback(error: Exception | None):
        """"Callback after playback ends ie. this is function called when playback ends."""
        if error:
            logger.error(f"[voice after] Error: {error!r}")
        # Instead of calling play_next directly (which is not thread-safe), we schedule it to be called in the event loop.
        bot.loop.call_soon_threadsafe(asyncio.create_task, _safe_play_next(bot, guild_id, vc))
    return _after_playback

async def _safe_play_next(bot: commands.Bot, guild_id: int, vc: discord.VoiceClient):
    """Async wrapper called from the 'after' callback."""
    try:
        queues[guild_id].pop(0)  # Remove the song that just finished
        await play_next(guild_id, vc)
    except Exception as e:
        logger.error(f"[play_next] crashed: {e!r}")

async def start_playback(guild_id: int, vc: discord.VoiceClient):
    """Start playback if idle."""
    if vc.is_playing() or vc.is_paused():
        logger.debug("Already playing or paused, not starting new playback")
        return
    logger.debug("Starting playback from queue")
    await play_next(guild_id, vc)

async def play_next(guild_id: int, vc: discord.VoiceClient):
    """Play next song in queue automatically."""
    logger.debug(f'Attempting to play next in queue for guild {guild_id}')
    try:
        if queues[guild_id] and len(queues[guild_id]) > 0:
            song = queues[guild_id][0]
            if song["stream_url"] == "":
                # Need to re-fetch stream_url
                song["stream_url"] = await get_stream_url(song["webpage_url"])

                info = await get_url_info(song["webpage_url"])
                song["stream_url"] = info.get("url")
                if not song["stream_url"]:
                    logger.error(f'Could not retrieve stream URL for {song["webpage_url"]}, skipping')
                    queues[guild_id].pop(0)
                    await play_next(guild_id, vc)
                    return
                
            logger.debug(f'Start playing: {song["title"]} (requested by {song["requester"]})')
            source = discord.FFmpegPCMAudio(
                song["stream_url"],
                before_options=FFMPEG_BEFORE,
                options=FFMPEG_OPTIONS,
            )        

            if vc.is_playing():
                logger.debug('Stopping current playback to play next in queue')
                vc.stop()
                # small pause to release FFmpeg process
                await asyncio.sleep(0.25)
            logger.debug('vc.play(...) called')
            vc.play(discord.PCMVolumeTransformer(source, volume=1.0), after=_after_factory(BOT, guild_id, vc))

            if len(queues[guild_id]) > 1:
                next_song = queues[guild_id][1]
                if next_song["stream_url"] == "":
                    next_song["stream_url"] = await get_stream_url(next_song["webpage_url"])
                    if not next_song["stream_url"]:
                        logger.error(f'Could not retrieve stream URL for next song {next_song["webpage_url"]}')
                        queues[guild_id].pop(1)  # remove the problematic next song

        else:
            logger.debug('Queue is empty, nothing to play next')
    except Exception as e:
        logger.error(f'Error in play_next: {e!r}')

def read_playlists():
    """Load playlists from disk if available."""
    global playlist_data

    playlists_path = Path("playlists")
    if not playlists_path.exists():
        playlists_path.mkdir()
        return

    for file in playlists_path.glob("*.json"):
        name = file.stem
        with file.open("r", encoding="utf-8") as f_in:
            playlist = json.load(f_in)
            playlist_data[name] = playlist
            logger.info(f"Loaded playlist '{name}' with {len(playlist)} songs")

def write_playlist(name: str, playlist: list[dict]):
    """Save playlist to disk."""
    playlists_path = Path("playlists")
    if not playlists_path.exists():
        playlists_path.mkdir()

    file_path = playlists_path / f"{name}.json"
    if playlist == [] and file_path.exists():
        file_path.unlink()
        logger.info(f"Deleted playlist file for '{name}'")
        return
    
    with file_path.open("w", encoding="utf-8") as f_out:
        json.dump(playlist, f_out, ensure_ascii=False, indent=4)
    logger.info(f"Saved playlist '{name}' with {len(playlist)} songs")

async def get_url_info(url: str) -> dict:
    """Get video info from URL using yt-dlp."""
    loop = asyncio.get_running_loop()
    with YoutubeDL(YDL_OPTIONS) as ydl:
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        if "entries" in info:  # playlist
            info = info["entries"][0]
        return info

async def get_stream_url(url: str) -> str:
    """Get direct stream URL from a YouTube URL."""
    info = await get_url_info(url)
    return info.get("url")

@BOT.event
async def on_ready():
    try:
        guild_id = get_secret_value('GUILD_ID')
        if guild_id:
            # Register all commands in this one guild (fast), THIS IS FOR TESTING ONLY !!
            guild = discord.Object(id=guild_id)
            BOT.tree.copy_global_to(guild=guild)  # optional if you also have @... (no guild)
            cmds = await BOT.tree.fetch_commands(guild=guild)
            for cmd in cmds:
                logger.debug(f"Found existing command: {cmd.name}")

            synced = await BOT.tree.sync(guild=guild)
            logger.info(f"✅ Synced {len(synced)} command(s) to guild {guild_id}")
        else:
            await BOT.tree.sync()
            logger.info(f"✅ Synced all global commands")

        logger.info(f"Kirjauduttu sisään: {BOT.user} (slash-komennot synkattu)")
    except Exception as e:
        logger.error(f"Slash-komentojen synkronointi epäonnistui: {e!r}")


# Commands ------------------------------------------------------------------------------------------------------------
#
@BOT.tree.command(description="Näytä ohje ja lista komennoista")
async def help(interaction: discord.Interaction):
    """Displays a help message with a list of all commands."""
    embed = discord.Embed(
        title="🎧 playBot Ohje",
        description="Tässä on lista käytettävissä olevista komennoista.",
        color=discord.Color.blurple()
    )

    # Playback Commands
    playback_commands = [
        "`/play <URL>` - Lisää soittojonoon YouTube-videon tai -soittolistan.",
        "`/queue` - Näyttää nykyisen soittojonon.",
        "`/skip` - Ohittaa nykyisen kappaleen.",
        "`/clear` - Tyhjennä soittojono.",        
        "`/shuffle` - Sekoittaa soittojonon (nykyinen kappale pysyy paikallaan)."
    ]
    embed.add_field(name="▶️ Toiston hallinta", value="\n".join(playback_commands), inline=False)

    # Playlist Commands
    playlist_commands = [
        "`/playlists` - Näyttää kaikki tallennetut soittolistat.",
        "`/create <nimi>` - Luo uuden, tyhjän soittolistan.",
        "`/show_playlist <nimi>` - Näyttää tietyn soittolistan kappaleet.",
        "`/delete_playlist <nimi>` - Poistaa soittolistan pysyvästi.",
        "`/remove_from_playlist <nimi> <numero>` - Poistaa kappaleen soittolistalta.",
        "`/add_to_playlist <nimi>` - Lisää nykyisen soittojonon kappaleet soittolistaan.",
        "`/play_playlist <nimi>` - Lisää soittolistan kappaleet soittojonoosi."
    ]
    embed.add_field(name="🎵 Soittolistat", value="\n".join(playlist_commands), inline=False)

    embed.set_footer(text=f"playBot | Pyydetty käyttäjältä {interaction.user.display_name}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@BOT.tree.command(description="Liity omaan äänikanavaasi")
async def join(interaction: discord.Interaction):
    try:
        await ensure_join_same_channel(interaction)
        await interaction.response.send_message("Liitytty kanavalle.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"En voinut liittyä: {e}", ephemeral=True)


@BOT.tree.command(description="Poistu äänikanavalta")
async def leave(interaction: discord.Interaction):
    vc = ensure_vc_for(interaction)
    if vc:
        logger.debug(f'Disconnecting from voice channel {vc.channel.name}')
        await vc.disconnect(force=True)
        await interaction.response.send_message("Poistuin äänikanavalta.", ephemeral=True)
    else:
        logger.debug('Not connected to any voice channel')
        await interaction.response.send_message("En ole äänikanavalla.", ephemeral=True)


@BOT.tree.command(description="Soita YouTube-linkki")
@app_commands.describe(url="YouTube-linkki")
async def play(interaction: discord.Interaction, url: str):
    try:
        logger.debug(f'Play command received with URL: {url}')
        
        vc = await ensure_join_same_channel(interaction)
        if not vc:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        info = await get_url_info(url)

        title = info.get("title", "Unknown")
        stream_url = info.get("url")
        webpage_url = info.get("webpage_url", url)

        guild_id = interaction.guild_id
        if guild_id not in queues:
            queues[guild_id] = []

        queues[guild_id].append({
            "title": title,
            "stream_url": stream_url,
            "webpage_url": webpage_url,
            "requester": interaction.user.display_name
        })

        logger.debug(f'Queued: {title} (lisännyt {interaction.user.display_name})')

        await interaction.followup.send(f"Lisätty soittolistalle: **{title}**")

        # Make sure playback is started or already ongoing
        await start_playback(interaction.guild_id, vc)

    except Exception as e:
        # If response is already deferred
        if interaction.response.is_done():
            await interaction.followup.send(f"Epäonnistui: {e}", ephemeral=False)
        else:
            await interaction.response.send_message(f"Epäonnistui: {e}", ephemeral=False)

@BOT.tree.command(description="Näytä soittolista (queue)")
async def queue(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    if guild_id not in queues or not queues[guild_id]:
        await interaction.response.send_message("📭 Soittojono on tyhjä")
        return

    # Build list of songs
    desc = ""
    for i, song in enumerate(queues[guild_id], start=1):
        desc += f"{i}. **{song['title']}** (lisännyt {song['requester']})\n"

    embed = discord.Embed(
        title="🎵 Soittojono",
        description=desc,
        color=discord.Color.blue()
    )

    await interaction.response.send_message(embed=embed)

@BOT.tree.command(description="Järjestä soittojono uudelleen")
async def shuffle(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    if guild_id not in queues or len(queues[guild_id]) <= 2:
        await interaction.response.send_message("Soittolista on tyhjö tai siinä on liian vähän kappaleita.")
        return

    # Shuffle the queue so that the currently playing song (first element) remains first
    now_playing = queues[guild_id][0]
    rest_of_queue = queues[guild_id][1:]
    random.shuffle(rest_of_queue)
    queues[guild_id] = [now_playing] + rest_of_queue

    await interaction.response.send_message("Soittolista on sekoitettu.")

@BOT.tree.command(description="Siirry seuraavaan kappaleeseen")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("Soittolista on tyhjä.")
        return
    song = queues.get(interaction.guild_id, [None])[0]
    if song:
        await interaction.response.send_message(f"⏭️ {interaction.user.display_name} on ohittamassa: **{song['title']}**")
        vc.stop()  # this triggers 'after', which will schedule play_next

@BOT.tree.command(description='Tyhjennä soittojono')
async def clear(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    if guild_id in queues:
        queues[guild_id] = []
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()  # this triggers 'after', which will schedule play_next (which will find the queue empty)
    await interaction.response.send_message("Soittojono tyhjennetty. 🤐")

@BOT.tree.command(description="Näytä tallennetut soittolistat")
async def playlists(interaction: discord.Interaction):
    if not playlist_data:
        await interaction.response.send_message("Ei saatavilla olevia soittolistoja")
        return
    
    desc = ""
    for name in playlist_data:
        desc += f"🎵 {name}\n"

    await interaction.response.send_message(f"Soittolistat:\n{desc}")

@BOT.tree.command(description="Luo soittolista")
@app_commands.describe(name="Soittolistan nimi")
async def create(interaction: discord.Interaction, name: str):
    logger.debug(f"Creating playlist: {name}")
    if name in playlist_data:
        await interaction.response.send_message(f"Soittolista '{name}' on jo olemassa.")
        return
    playlist_data[name] = []
    await interaction.response.send_message(f"Lisättiin uusi soittolista: '{name}'")

@BOT.tree.command(description="Näytä soittolistan kappaleet")
@app_commands.describe(name="Soittolistan nimi")
async def show_playlist(interaction: discord.Interaction, name: str):
    if name not in playlist_data:
        await interaction.response.send_message(f"Soittolista '{name}' ei ole olemassa.")
        return

    playlist = playlist_data[name]
    if not playlist:
        await interaction.response.send_message(f"Soittolista '{name}' on tyhjä.")
        return

    desc = ""
    for i, song in enumerate(playlist, start=1):
        desc += f"{i}. **{song['title']}** (lisännyt {song['requester']})\n"

    embed = discord.Embed(
        title=f"🎵 Soittolista: {name}",
        description=desc,
        color=discord.Color.green()
    )

    await interaction.response.send_message(embed=embed)

@BOT.tree.command(description="Poista soittolista")
@app_commands.describe(name="Soittolistan nimi")
async def delete_playlist(interaction: discord.Interaction, name: str):
    if name not in playlist_data:
        await interaction.response.send_message(f"Soittolista '{name}' ei ole olemassa.")
        return
    del playlist_data[name]
    write_playlist(name, [])  # remove from disk
    await interaction.response.send_message(f"Soittolista poistettu: '{name}'")

@BOT.tree.command(description="Poista kappale soittolistalta sen numeron perusteella")
@app_commands.describe(name="Soittolistan nimi", number="Kappaleen numero soittolistalla (1-pohjainen)")
async def remove_from_playlist(interaction: discord.Interaction, name: str, number: int):
    if name not in playlist_data:
        await interaction.response.send_message(f"Soittolista '{name}' ei ole olemassa.")
        return

    playlist = playlist_data[name]
    if number < 1 or number > len(playlist):
        await interaction.response.send_message(f"Virheellinen kappalenumero. Soittolistalla '{name}' on {len(playlist)} kappaletta.")
        return

    song = playlist.pop(number - 1)
    write_playlist(name, playlist)
    await interaction.response.send_message(f"Poistettu soittolistalta: '{name}': **{song['title']}**")
    
@BOT.tree.command(description="Lisää nykyinen soittojono soittolistalle")
@app_commands.describe(name="Soittolistan nimi")
async def add_to_playlist(interaction: discord.Interaction, name: str):
    if name not in playlist_data:
        await interaction.response.send_message(f"Soittolista '{name}' ei ole olemassa.")
        return

    if not queues.get(interaction.guild_id):
        await interaction.response.send_message("Soittojono on tyhjö.")
        return

    # extend playlist with current queue, avoiding duplicates by "stream_url"
    playlist = playlist_data.setdefault(name, [])
    queue = queues.get(interaction.guild_id, [])

    # build a set of already existing stream_urls for quick lookup
    existing_urls = {item["webpage_url"] for item in playlist}
    
    added_count = 0
    for song in queue:
        if song["webpage_url"] not in existing_urls:
            song["stream_url"] = ""  # clear stream_url to force re-fetching when played from playlist
            playlist.append(song)
            existing_urls.add(song["webpage_url"])
            added_count += 1

    write_playlist(name, playlist)
    await interaction.response.send_message(f"Soittojonosta lisätty {added_count} kappaletta soittolistalle: '{name}'")

@BOT.tree.command(description="Lisää soittolista soittojono")
@app_commands.describe(name="Soittolistan nimi")
async def play_playlist(interaction: discord.Interaction, name: str):
    if name not in playlist_data:
        await interaction.response.send_message(f"Soittolista '{name}' ei ole olemassa.")
        return

    playlist = playlist_data[name]
    if not playlist:
        await interaction.response.send_message(f"Soittolista '{name}' on tyhjä.")
        return

    try:
        vc = await ensure_join_same_channel(interaction)
        if not vc:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild_id = interaction.guild_id
        if guild_id not in queues:
            queues[guild_id] = []

        # Append all songs from the playlist to the queue, avoiding duplicates by "webpage_url"
        existing_urls = {item["webpage_url"] for item in queues[guild_id]}
        added_count = 0
        for song in playlist:
            if song["webpage_url"] not in existing_urls:
                song["stream_url"] = ""
                queues[guild_id].append(song)
                existing_urls.add(song["webpage_url"])
                added_count += 1

        await interaction.followup.send(f"Lisätty {added_count} kappaletta soittolistalta '{name}' soittojonoon.")

        # Make sure playback is started or already ongoing
        await start_playback(interaction.guild_id, vc)

    except Exception as e:
        # If response is already deferred
        if interaction.response.is_done():
            await interaction.followup.send(f"Epäonnistui: {e}", ephemeral=False)
        else:
            await interaction.response.send_message(f"Epäonnistui: {e}", ephemeral=False)    

if __name__ == "__main__":
    # Setup simple logging
    level = get_secret_value('LOG_LEVEL', 'INFO')
    if level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        level = 'INFO'

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s",
        datefmt="%d.%m.%Y %H:%M:%S"
    )

    logger = logging.getLogger(__name__)

    ensure_opus_loaded()

    # Discord token for the bot
    TOKEN = get_secret_value('DISCORD_TOKEN')
    if not TOKEN:
        logger.error("DISCORD_TOKEN is missing from settings (environment variable or Docker secret)")
        raise SystemExit("DISCORD_TOKEN is missing from settings (environment variable or Docker secret)")

    YDL_OPTIONS = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,       # Handle only single videos
        "default_search": "auto", # If you ever want to search by keywords
    }

    # Ffmpeg options
    FFMPEG_BEFORE = (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    )
    FFMPEG_OPTIONS = "-vn"  # No video

    # Object for managing audio queues at the guild level
    queues = {}

    read_playlists()

    BOT.run(TOKEN)
