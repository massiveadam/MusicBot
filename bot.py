import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import asyncio
import aiohttp
from pathlib import Path
import uuid
from typing import Dict, List, Optional
import time

import subprocess
import requests
from mutagen import File as MutagenFile
from universal_scraper import extract_metadata  # async for /ripurl and /save
from colorthief import ColorThief
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image
from urllib.parse import urlparse
import re
import xml.etree.ElementTree as ET
import unicodedata
from fuzzywuzzy import fuzz
import math
import html
import datetime
import logging
import urllib.parse


# Configuration management - this is a test
class Config:
    """Configuration class for the Discord bot."""
    
    def __init__(self):
        self.TOKEN = os.getenv("DISCORD_TOKEN")
        self.MUSIC_FOLDER = os.getenv("MUSIC_FOLDER", "/music")
        self.DOWNLOADS_FOLDER = os.getenv("DOWNLOADS_FOLDER", "/downloads")
        self.DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "1036854855844757505"))
        self.PLEX_TOKEN = os.getenv("PLEX_TOKEN")
        self.PLEX_URL = os.getenv("PLEX_URL")
        self.PLEX_MACHINE_ID = None
        self.GAMDL_CODEC = os.getenv("GAMDL_CODEC", "aac-legacy")
        self.COOKIES_PATH = os.getenv("COOKIES_PATH", "/app/cookies.txt")
        self.DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
        self.RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
        
        # Validate critical configuration
        if not self.TOKEN:
            raise ValueError("DISCORD_TOKEN environment variable is required")
            
    @property
    def DEV_GUILD(self):
        return discord.Object(id=self.DEV_GUILD_ID)

config = Config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# Listening Room Classes
class ListeningRoom:
    """Represents an active listening room session."""
    
    def __init__(self, host: discord.Member, guild: discord.Guild, artist: str, album: str, source_type: str, source_data: str):
        self.room_id = str(uuid.uuid4())[:8]  # Short ID for easy reference
        self.host = host
        self.guild = guild
        self.artist = artist
        self.album = album
        self.source_type = source_type  # 'local' or 'apple_music'
        self.source_data = source_data  # file path or URL
        
        # Channels
        self.voice_channel: Optional[discord.VoiceChannel] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        
        # Participants
        self.participants: List[discord.Member] = [host]
        self.max_participants = 5
        
        # Playback state
        self.current_track = 0
        self.tracks: List[Dict] = []  # List of track info dicts
        self.is_playing = False
        self.start_time = None
        self.pause_time = None
        
        # Created timestamp
        self.created_at = time.time()
        
    @property
    def is_full(self) -> bool:
        return len(self.participants) >= self.max_participants
        
    @property
    def current_track_info(self) -> Optional[Dict]:
        if 0 <= self.current_track < len(self.tracks):
            return self.tracks[self.current_track]
        return None
        
    def add_participant(self, member: discord.Member) -> bool:
        """Add a participant to the room. Returns True if successful."""
        if member in self.participants:
            return False
        if self.is_full:
            return False
        self.participants.append(member)
        return True
        
    def remove_participant(self, member: discord.Member) -> bool:
        """Remove a participant from the room. Returns True if successful."""
        if member in self.participants:
            self.participants.remove(member)
            return True
        return False


class ListeningRoomManager:
    """Manages all active listening rooms."""
    
    def __init__(self):
        self.rooms: Dict[str, ListeningRoom] = {}  # room_id -> ListeningRoom
        self.user_rooms: Dict[int, str] = {}  # user_id -> room_id
        
    def create_room(self, host: discord.Member, guild: discord.Guild, artist: str, album: str, source_type: str, source_data: str) -> ListeningRoom:
        """Create a new listening room."""
        # Remove user from any existing room first
        self.leave_room(host)
        
        room = ListeningRoom(host, guild, artist, album, source_type, source_data)
        self.rooms[room.room_id] = room
        self.user_rooms[host.id] = room.room_id
        
        logger.info(f"Created listening room {room.room_id} for {artist} - {album}")
        return room
        
    def get_room(self, room_id: str) -> Optional[ListeningRoom]:
        """Get a room by ID."""
        return self.rooms.get(room_id)
        
    def get_user_room(self, user_id: int) -> Optional[ListeningRoom]:
        """Get the room a user is currently in."""
        room_id = self.user_rooms.get(user_id)
        if room_id:
            return self.rooms.get(room_id)
        return None
        
    def join_room(self, room_id: str, member: discord.Member) -> bool:
        """Add a user to a room. Returns True if successful."""
        room = self.get_room(room_id)
        if not room:
            return False
            
        # Remove from current room first
        self.leave_room(member)
        
        if room.add_participant(member):
            self.user_rooms[member.id] = room_id
            logger.info(f"User {member.name} joined room {room_id}")
            return True
        return False
        
    async def leave_room(self, member: discord.Member) -> Optional[str]:
        """Remove a user from their current room. Returns room_id if they were in one."""
        current_room_id = self.user_rooms.get(member.id)
        if not current_room_id:
            return None
            
        room = self.rooms.get(current_room_id)
        if room:
            room.remove_participant(member)
            del self.user_rooms[member.id]
            
            # If room is empty, clean it up
            if len(room.participants) == 0:
                await self.cleanup_room(current_room_id)
                
            logger.info(f"User {member.name} left room {current_room_id}")
            return current_room_id
        return None
        
    async def cleanup_room(self, room_id: str):
        """Clean up an empty room and delete Discord channels."""
        room = self.rooms.get(room_id)
        if room:
            # Clean up any remaining user mappings
            users_to_remove = [user_id for user_id, rid in self.user_rooms.items() if rid == room_id]
            for user_id in users_to_remove:
                del self.user_rooms[user_id]
            
            # Delete Discord channels
            try:
                if room.voice_channel:
                    await room.voice_channel.delete(reason="Listening room ended")
                    logger.info(f"Deleted voice channel for room {room_id}")
                    
                if room.text_channel:
                    # Send farewell message before deleting
                    await room.text_channel.send("🎵 **Listening room ended.** Thanks for listening together! 👋")
                    await asyncio.sleep(2)  # Give people time to see the message
                    await room.text_channel.delete(reason="Listening room ended")
                    logger.info(f"Deleted text channel for room {room_id}")
                    
                # Disconnect voice client if connected
                if room.voice_client:
                    await room.voice_client.disconnect()
                    
            except Exception as e:
                logger.error(f"Error cleaning up channels for room {room_id}: {e}")
                
            del self.rooms[room_id]
            logger.info(f"Cleaned up room {room_id}")
            
    async def cleanup_all_rooms(self):
        """Clean up all active rooms (admin function)."""
        room_ids = list(self.rooms.keys())
        for room_id in room_ids:
            await self.cleanup_room(room_id)
        logger.info(f"Cleaned up {len(room_ids)} rooms")
            
    def get_all_rooms(self) -> List[ListeningRoom]:
        """Get all active rooms."""
        return list(self.rooms.values())


# Global room manager
room_manager = ListeningRoomManager()

intents = discord.Intents.default()
intents.message_content = True  # Required for reading mentions in messages
intents.members = True  # ✅ add this line
saved_embeds = {}  # message_id: { "url": str, "user_id": int }
bot = commands.Bot(command_prefix="!", intents=intents)


def validate_url(url: str) -> bool:
    """Validate if the provided string is a valid URL."""
    try:
        result = urllib.parse.urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False


def sanitize_filename(filename: str) -> str:
    """Sanitize a string to be safe for use as a filename."""
    # Remove or replace invalid characters
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename.strip()


def sanitize_query(query: str) -> str:
    """Sanitize search query for safe use in URLs."""
    return urllib.parse.quote_plus(query.strip())


async def handle_rip_logic(interaction, url: str):
    """Unified download logic for all music sources"""
    # If already an Apple Music link, use it directly
    if "music.apple.com" in url:
        apple_url = url
    # If from a known streaming site, resolve via Odesli
    elif any(site in url for site in ["spotify.com", "tidal.com", "deezer.com"]):
        apple_url = await fetch_apple_url(url)
    # If from a blog-style site, extract metadata and resolve streaming link
    else:
        try:
            metadata = await extract_metadata(url)
            artist = metadata.get("artist")
            album = metadata.get("album")
            links = metadata.get("links", {})

            # Prefer resolving through Apple Music or Spotify link if available
            preferred_link = None
            if "appleMusic" in links:
                preferred_link = links["appleMusic"]["url"]
            elif "spotify" in links:
                preferred_link = links["spotify"]["url"]

            if not preferred_link:
                await interaction.followup.send("❌ Could not find a streaming link for this album.")
                return

            apple_url = await fetch_apple_url(preferred_link)
        except Exception as e:
            logger.error(f"Failed to extract metadata or resolve streaming URL for {url}: {e}")
            await interaction.followup.send("❌ Failed to extract metadata or resolve streaming URL.")
            return

    if not apple_url:
        await interaction.followup.send("❌ Could not resolve an Apple Music link from that URL.")
        return

    await download_album(interaction, apple_url)

def build_streaming_buttons(links):
    view = discord.ui.View()
    if "appleMusic" in links:
        view.add_item(discord.ui.Button(label="Apple Music", url=links["appleMusic"]["url"]))
    if "bandcamp" in links:
        view.add_item(discord.ui.Button(label="Bandcamp", url=links["bandcamp"]["url"]))
    return view


async def fetch_apple_url(odesli_url):
    encoded = aiohttp.helpers.quote(odesli_url, safe="")
    api_url = f"https://api.song.link/v1-alpha.1/links?url={encoded}"
    async with aiohttp.ClientSession() as session:
        async with session.get(api_url) as resp:
            if resp.status != 200:
                print(f"[WARN fetch_apple_url] Request failed: {resp.status}")
                return None
            data = await resp.json()
            return data.get("linksByPlatform", {}).get("appleMusic", {}).get("url")


async def run_beet_import(use_autotag: bool = False, path: str = None):
    """Import downloads using local beets installation.
    
    Args:
        use_autotag: If False, uses -A flag to skip auto-tagging. If True, enables auto-tagging.
        path: Custom path to import from. Defaults to DOWNLOADS_FOLDER.
    """
    import_path = path or config.DOWNLOADS_FOLDER
    
    try:
        # Build command arguments
        cmd_args = ["beet", "import"]
        
        if not use_autotag:
            cmd_args.append("-A")  # Skip auto-tagging, just move files
            logger.info("Running beets import with auto-tagging disabled (-A flag)")
        else:
            logger.info("Running beets import with auto-tagging enabled")
            # Add flags for better auto-tagging experience
            cmd_args.extend(["-q", "--noninteractive"])  # Quiet, non-interactive mode
        
        cmd_args.append(import_path)
        
        # Run beets import command
        process = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            logger.info("Beets import completed successfully.")
            return True
        else:
            error_output = stderr.decode() if stderr else "Unknown error"
            logger.error("Beets import failed: %s", error_output)
            return False
            
    except Exception as e:
        logger.error("Exception during beets import: %s", e)
        return False


async def run_gamdl(
    url: str,
    cookies_path: str = None,
    codec: str = None,
    remux_mode: str = "mp4box",
    output_path: str = None
) -> tuple[int, str]:
    """Run gamdl with AAC-legacy only (your proven working method)"""
    cookies_path = cookies_path or config.COOKIES_PATH
    codec = codec or config.GAMDL_CODEC
    output_path = output_path or config.DOWNLOADS_FOLDER
    
    logger.info(f"🎧 Using {codec} codec")
    
    process = await asyncio.create_subprocess_exec(
        "gamdl",
        "--cookies-path", cookies_path,
        "--codec-song", codec,
        "--remux-mode", remux_mode,
        "--output-path", output_path,
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    stdout, _ = await process.communicate()
    output = stdout.decode("utf-8").strip()
    
    if process.returncode == 0:
        logger.info(f"✅ Download succeeded with AAC-legacy")
    else:
        logger.warning(f"❌ Failed with AAC-legacy: {process.returncode}")
        logger.warning(f"Output: {output}")
    
    return process.returncode, output


async def download_album(interaction, url):
    artist = "Unknown Artist"
    album = "Unknown Album"

    try:
        metadata = await extract_metadata(url)
        artist = metadata.get("artist", "Unknown Artist")
        album = metadata.get("album", "Unknown Album")
    except Exception as e:
        logger.warning(f"Could not extract metadata from URL {url}: {e}")
        artist = "Unknown Artist"
        album = "Unknown Album"

    # Send download message based on interaction type
    if hasattr(interaction, "response") and hasattr(interaction.response, "send_message"):
        await interaction.response.send_message(f"📥 Downloading **{artist} - {album}**...", ephemeral=True)
    else:
        await interaction.channel.send(f"📥 Downloading **{artist} - {album}**...")

    # Start download process with GAMDL (AAC-legacy only)
    returncode, output = await run_gamdl(url)

    if returncode != 0:
        error_msg = output.strip() or "Unknown error"
        logger.error("GAMDL failed: %s", error_msg)
        await interaction.channel.send(f"❌ Download failed:\n```{error_msg}```")
        return

    # Import with beets
    await interaction.channel.send("📀 Importing with beets...")
    
    beets_success = await run_beet_import()
    if not beets_success:
        await interaction.channel.send("⚠️ Download completed, but beets import failed. Check logs for details.")
        return

    # Progressive retry logic for Plex album search
    plex_url = None
    wait_times = [3, 5, 7]  # Progressive wait strategy
    
    for i, wait_time in enumerate(wait_times):
        if i > 0:  # Don't wait before first attempt
            await interaction.channel.send(f"📀 Waiting for Plex to scan new files... ({wait_time}s)")
            await asyncio.sleep(wait_time)
        
        plex_url = await get_plex_album_guid(artist, album)
        if plex_url:
            logger.info("Found album in Plex after %d attempts", i + 1)
            break
    
    if not plex_url:
        await interaction.channel.send("⚠️ Downloaded and imported, but album not found in Plex. It may take longer to appear.")
        return

    # Build and send album embed
    try:
        embed = discord.Embed(
            title=album,
            description=f"**Artist:** {artist}\n\n✅ Available on Plex",
            color=discord.Color.green()
        )
        
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Plexamp", url=plex_url))
        
        await interaction.channel.send(embed=embed, view=view)
    except Exception as e:
        logger.error("Failed to build album embed: %s", e)
        await interaction.channel.send(f"✅ **{artist} - {album}** downloaded and imported successfully!")

    # Mirror to #music-town if needed
    music_town = discord.utils.get(interaction.guild.text_channels, name="music-town")
    if music_town and music_town.permissions_for(interaction.guild.me).send_messages:
        await music_town.send(embed=embed, view=view)


def extract_metadata_from_path(path):
    """Extract artist and album from file path."""
    parts = Path(path).parts
    # expected: /downloads/Artist/Album
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    return "Unknown Artist", Path(path).name


def extract_cover_art_thumbnail(path):
    """Find cover art file in the given directory."""
    try:
        for root, _, files in os.walk(path):
            for file in files:
                if file.lower().endswith((".jpg", ".jpeg", ".png")) and "cover" in file.lower():
                    return os.path.join(root, file)
        return None
    except Exception as e:
        logger.warning(f"Failed to find cover art in {path}: {e}")
        return None


async def wait_for_album_in_music(artist, album_title, timeout=60):
    """Wait for album to appear in music directory."""
    target = f"/music/{artist}/{album_title}"
    for _ in range(timeout):
        if os.path.exists(target):
            return target
        await asyncio.sleep(1)
    return None


async def wait_for_album_path(base_path, artist, album, timeout=200):
    """Asynchronously wait for Beets to move the album into /music."""
    album_path = Path("/music") / artist / album
    elapsed = 0

    while not album_path.exists() and elapsed < timeout:
        await asyncio.sleep(1)
        elapsed += 1

    return str(album_path) if album_path.exists() else None


def get_latest_album_dir(base_path: Path) -> Path | None:
    """Find the most recently modified directory containing music files."""
    try:
        dirs = sorted(base_path.rglob("*"), key=os.path.getmtime, reverse=True)
        for d in dirs:
            if d.is_dir() and any(f.suffix in [".flac", ".mp3", ".m4a"] for f in d.glob("*")):
                return d
        return None
    except Exception as e:
        logger.warning(f"Failed to find latest album directory in {base_path}: {e}")
        return None


def extract_file_metadata(album_dir: Path):
    files = list(album_dir.glob("*.flac")) + list(album_dir.glob("*.mp3"))
    if not files:
        return None, None, [], None

    def get_tag(meta, key, default):
        if not meta:
            return default
        return meta.get(key, [default])[0] if isinstance(meta.get(key), list) else meta.get(key, default)

    metadata = MutagenFile(files[0], easy=True)
    artist = get_tag(metadata, "artist", "Unknown Artist")
    album = get_tag(metadata, "album", "Unknown Album")

    track_titles = []
    max_tracks = 5
    for i, file in enumerate(files[:max_tracks]):
        meta = MutagenFile(file, easy=True)
        title = get_tag(meta, "title", file.stem)
        track_titles.append(f"{i+1}. {title}")

    if len(files) > max_tracks:
        track_titles.append("...")

    cover_path = album_dir / "cover.jpg"
    return artist, album, track_titles, cover_path


def normalize(text):
    if not text:
        return ""
    return unicodedata.normalize("NFKD", text.lower()).strip()


async def get_plex_album_guid(artist: str, album: str) -> str | None:
    if not config.PLEX_TOKEN or not config.PLEX_URL:
        return None

    headers = {"X-Plex-Token": config.PLEX_TOKEN}
    query = album

    try:
        async with aiohttp.ClientSession() as session:
            search_url = f"{config.PLEX_URL}/library/search?query={query}"
            async with session.get(search_url, headers=headers) as response:
                if response.status != 200:
                    return None
                xml_text = await response.text()

        root = ET.fromstring(xml_text)
        best_score = 0
        best_guid = None

        for elem in root.findall(".//Directory") + root.findall(".//Video"):
            if elem.attrib.get("type") != "album":
                continue
            title = elem.attrib.get("title", "")
            parent = elem.attrib.get("parentTitle", "")
            guid_raw = elem.attrib.get("guid", "")

            score = fuzz.token_sort_ratio(f"{artist} {album}", f"{parent} {title}")
            if score > best_score:
                best_score = score
                match = re.search(r"plex://album/([^?]+)", guid_raw)
                if match:
                    best_guid = match.group(1)

        if best_score >= 85 and best_guid:
            if config.PLEX_MACHINE_ID:
                return f"https://listen.plex.tv/album/{best_guid}?source={config.PLEX_MACHINE_ID}"
            else:
                return f"https://app.plexamp.com/album/{best_guid}"
        return None

    except Exception as e:
        print("[WARN get_plex_album_guid]", e)
        return None


async def build_album_embed(url: str, plex_url: str = None):
    metadata = await extract_metadata(url)
    artist = metadata["artist"]
    album = metadata["album"]
    cover = metadata["cover_url"]
    links = metadata["links"]
    source_url = metadata["source"]
    domain = urlparse(source_url).netloc

    source_name = (
        "Pitchfork" if "pitchfork" in domain else
        "The Quietus" if "thequietus" in domain else
        "Album of the Year" if "albumoftheyear" in domain else
        "Rate Your Music" if "rateyourmusic" in domain else
        "Bandcamp" if "bandcamp" in domain else
        domain
    )

    embed_color = discord.Color.purple()
    try:
        if cover:
            async with aiohttp.ClientSession() as session:
                async with session.get(cover, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        content = await response.read()
                        dominant = ColorThief(BytesIO(content)).get_color(quality=1)
                        embed_color = discord.Color.from_rgb(*dominant)
    except Exception as e:
        logger.warning(f"Could not extract color from cover {cover}: {e}")

    description = f"{artist}\n\n**Source:** [{source_name}]({source_url})"
    view = discord.ui.View()

    # ✅ Only call get_plex_album_guid if no plex_url was passed in
    if plex_url is None:
        plex_url = await get_plex_album_guid(artist, album)

    if plex_url:
        view.add_item(discord.ui.Button(label="Plexamp", url=plex_url))
        description += "\n\n*Already in your library*"
    else:
        if "appleMusic" in links:
            view.add_item(discord.ui.Button(label="Apple Music", url=links["appleMusic"]["url"]))
        if "bandcamp" in links:
            view.add_item(discord.ui.Button(label="Bandcamp", url=links["bandcamp"]["url"]))

    embed = discord.Embed(
        title=f"**{album}**",
        description=description,
        color=embed_color
    )
    if cover and cover.startswith("http"):
        embed.set_thumbnail(url=cover)

    print(f"[DEBUG] Embed created: {artist} - {album}, source: {source_name}")
    return embed, view, artist, album, links


async def fetch_aoty_trending(limit: int = 5):
    # Use the AOTY "This Week" releases page instead of the Discover page
    url = (
        "https://api.allorigins.win/raw?url=https%3A%2F%2Fwww.albumoftheyear.org%2F"
        "releases%2Fthis-week%2F"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
        soup = BeautifulSoup(text, "html.parser")
        albums = []
        for block in soup.select("div.albumBlock")[:limit]:
            link = block.find("a")
            if not link:
                continue
            album_url = "https://www.albumoftheyear.org" + link.get("href")
            album = block.select_one("div.albumTitle")
            artist = block.select_one("div.artistTitle")
            img = block.find("img")
            albums.append({
                "artist": artist.text.strip() if artist else "Unknown",
                "album": album.text.strip() if album else "Unknown",
                "url": album_url,
                "cover": img.get("src") if img else None,
            })
        return albums
    except Exception as e:
        print("[WARN fetch_aoty_trending]", e)
        return []


async def fetch_bandcamp_aotd(limit: int = 1):
    """Retrieve the latest Bandcamp Album(s) of the Day."""
    url = "https://daily.bandcamp.com/feed"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()

        root = ET.fromstring(text)
        albums = []
        for item in root.findall("./channel/item"):
            categories = [c.text for c in item.findall("category")]
            if "Album of the Day" not in categories:
                continue
            title = item.findtext("title") or "Unknown"
            link = item.findtext("link") or ""
            description = item.findtext("description") or ""
            img_match = re.search(r'<img src="([^"]+)"', description)
            img = img_match.group(1) if img_match else None
            m = re.match(r"(.+?), [\"\u201C](.+?)[\"\u201D]", title)
            if m:
                artist, album = m.group(1), m.group(2)
            elif " - " in title:
                artist, album = title.split(" - ", 1)
            else:
                artist = album = title
            albums.append({"artist": artist, "album": album, "url": link, "cover": img})
            if len(albums) >= limit:
                break
        return albums
    except Exception as e:
        print("[WARN fetch_bandcamp_aotd]", e)
        return []


async def fetch_quietus_aotw(limit: int = 5):
    """Fetch The Quietus Album of the Week entries."""
    url = "https://thequietus.com/columns/quietus-reviews/album-of-the-week/feed"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()

        root = ET.fromstring(text)

        def parse_title(t: str) -> tuple[str | None, str]:
            t = html.unescape(t)
            t = re.sub(r"\s+is\s+our\s+album\s+of\s+the\s+week$", "", t, flags=re.I)
            if ":" in t:
                t = t.split(":", 1)[1].strip()
            if " by " in t:
                album, artist = t.rsplit(" by ", 1)
                return artist.strip(), album.strip()
            m = re.search(r"([\w\s]+?)['']s\s+(.+)", t)
            if m:
                return m.group(1).strip(), m.group(2).strip()
            tokens = t.split()
            if len(tokens) >= 2:
                return tokens[0].strip(), " ".join(tokens[1:]).strip()
            return None, t.strip()

        albums = []
        for item in root.findall("./channel/item"):
            title = item.findtext("title") or "Unknown"
            link = item.findtext("link") or ""
            description = item.findtext("description") or ""
            img_match = re.search(r"<img[^>]+src=\"([^\"]+)\"", description)
            img = img_match.group(1) if img_match else None
            artist, album = parse_title(title)
            albums.append(
                {
                    "artist": artist or "Unknown",
                    "album": album,
                    "url": link,
                    "cover": img,
                }
            )
            if len(albums) >= limit:
                break
        return albums
    except Exception as e:
        print("[WARN fetch_quietus_aotw]", e)
        return []


async def fetch_pitchfork_best_new(limit: int = 5):
    url = "https://r.jina.ai/https://pitchfork.com/reviews/best/albums/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
        lines = text.splitlines()
        albums = []
        for i, line in enumerate(lines):
            if line.startswith("[![Image") and "/photos/" in line:
                img_match = re.search(r"\((https?://[^)]+)\)", line)
                img = img_match.group(1) if img_match else None
                for j in range(i + 1, len(lines)):
                    if lines[j].startswith("[###"):
                        m = re.search(r"\[### _([^_]+)_\]\(([^)]+)\)", lines[j])
                        if m:
                            album = m.group(1)
                            album_url = m.group(2)
                            k = j + 1
                            while k < len(lines) and not lines[k].strip():
                                k += 1
                            artist = lines[k].strip() if k < len(lines) else "Unknown"
                            albums.append({
                                "artist": artist,
                                "album": album,
                                "url": album_url,
                                "cover": img,
                            })
                        break
                if len(albums) >= limit:
                    break
        return albums
    except Exception as e:
        print("[WARN fetch_pitchfork_best_new]", e)
        return []


async def fetch_brooklynvegan_notable(limit: int = 10):
    """Fetch the latest BrooklynVegan Notable Releases of the Week."""
    list_url = (
        "https://r.jina.ai/https://www.brooklynvegan.com/category/music/new-releases/"
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(list_url) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
        match = re.search(
            r"\[Notable Releases of the Week[^\]]*\]\((https:[^)]+)\)", text
        )
        if not match:
            return []
        article_url = match.group(1)

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"https://r.jina.ai/{article_url}") as resp:
                if resp.status != 200:
                    return []
                article_text = await resp.text()

        pattern = re.compile(r"\*\*(.*?) - _([^_]+)_\*\*")
        albums = []
        for artist, album in pattern.findall(article_text):
            artist = artist.strip()
            album = album.strip()
            if not artist or not album:
                continue
            albums.append({
                "artist": artist,
                "album": album,
                "url": article_url,
                "cover": None,
            })
            if len(albums) >= limit:
                break

        return albums
    except Exception as e:
        print("[WARN fetch_brooklynvegan_notable]", e)
        return []


async def create_collage(urls: list[str], cell_size: int = 100, columns: int = 3) -> BytesIO | None:
    if not urls:
        return None

    async def fetch(session, url):
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return BytesIO(await resp.read())
        except Exception as e:
            print("[WARN create_collage fetch]", e)
        return None

    async with aiohttp.ClientSession() as session:
        images_bytes = await asyncio.gather(*(fetch(session, u) for u in urls))

    images = []
    for data in images_bytes:
        if isinstance(data, BytesIO):
            try:
                img = Image.open(data).convert("RGB")
                images.append(img)
            except Exception as e:
                print("[WARN create_collage open]", e)

    if not images:
        return None

    columns = max(1, min(columns, len(images)))
    rows = math.ceil(len(images) / columns)
    collage = Image.new("RGB", (columns * cell_size, rows * cell_size))
    for idx, img in enumerate(images):
        img = img.resize((cell_size, cell_size))
        x = (idx % columns) * cell_size
        y = (idx // columns) * cell_size
        collage.paste(img, (x, y))

    output = BytesIO()
    collage.save(output, format="JPEG")
    output.seek(0)
    return output



@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return  # Ignore bot's own reactions

    guild = bot.get_guild(payload.guild_id)
    channel = guild.get_channel(payload.channel_id)
    member = guild.get_member(payload.user_id)

    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        print(f"[ERROR] Could not fetch message: {e}")
        return

    emoji = str(payload.emoji)
    if message.id not in saved_embeds:
        return  # Not one of our tracked messages

    url = saved_embeds[message.id]["url"]

    # Create fake interaction with message + user (for download/save/etc)
    try:
        fake_interaction = FakeInteraction(message=message, user=member)
    except Exception as e:
        print(f"[ERROR] Failed to create FakeInteraction: {e}")
        return

    if emoji == "📥":
        await handle_rip_logic(fake_interaction, url)
    elif emoji == "📌":
        await handle_save_logic(fake_interaction, url)
    elif emoji == "❌":
        await message.delete()
    elif emoji == "🎧":
        await mark_as_listened(fake_interaction, message)
    elif emoji == "🔁":
        await recommend_album(fake_interaction, message)



@bot.event
async def on_ready():
    try:
        bot.tree.clear_commands(guild=config.DEV_GUILD)
        print("[INFO] Cleared existing guild commands")
        bot.tree.copy_global_to(guild=config.DEV_GUILD)
        await bot.tree.sync(guild=config.DEV_GUILD)
        print(f"[INFO] Synced commands to dev guild {config.DEV_GUILD_ID}")
    except Exception as e:
        print(f"[WARN] Failed to sync commands: {e}")
    print(f"[INFO] Logged in as {bot.user}")
    print(f"[INFO] Using music folder: {config.MUSIC_FOLDER}")

    if config.PLEX_TOKEN and config.PLEX_URL:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{config.PLEX_URL}/?X-Plex-Token={config.PLEX_TOKEN}") as resp:
                    if resp.status == 200:
                        xml = await resp.text()
                        match = re.search(r'machineIdentifier="([^"]+)"', xml)
                        if match:
                            config.PLEX_MACHINE_ID = match.group(1)
                            print(f"[INFO] Found Plex machine ID: {config.PLEX_MACHINE_ID}")
        except Exception as e:
            print(f"[WARN] Could not fetch Plex machine ID: {e}")

    if not scheduled_hotupdates.is_running():
        scheduled_hotupdates.start()

@bot.tree.command(name="testembed", description="Preview album embed from a source URL")
@app_commands.describe(url="Link to Bandcamp, AOTY, Pitchfork, etc.")
async def testembed(interaction: discord.Interaction, url: str):
    if not validate_url(url):
        await interaction.response.send_message("❌ Invalid URL provided.", ephemeral=True)
        return
        
    await interaction.response.defer(thinking=True, ephemeral=True)
    embed, view, _, _, _ = await build_album_embed(url)
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="rip", description="Download album from any music source")
@app_commands.describe(url="Music URL (Apple Music, Spotify, TIDAL, Bandcamp, blog reviews, etc.)")
async def rip(interaction: discord.Interaction, url: str):
    if not validate_url(url):
        await interaction.response.send_message("❌ Invalid URL provided.", ephemeral=True)
        return
        
    await interaction.response.defer(thinking=True)
    await handle_rip_logic(interaction, url)


@bot.tree.command(name="save", description="Save an album for later listening")
@app_commands.describe(url="A link to Bandcamp, Pitchfork, AOTY, etc.")
async def save(interaction: discord.Interaction, url: str):
    if not validate_url(url):
        await interaction.response.send_message("❌ Invalid URL provided.", ephemeral=True)
        return
        
    is_fake = isinstance(interaction, FakeInteraction)

    if not is_fake:
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except discord.InteractionResponded:
            pass  # Already responded

    try:
        await handle_save_logic(interaction, url)
    except Exception as e:
        print("[ERROR] /save failed:", e)
        if not is_fake:
            try:
                await interaction.followup.send("❌ Failed to save the album.")
            except:
                pass
        return

    # ✅ Clean up the "thinking..." indicator
    if not is_fake:
        try:
            await interaction.edit_original_response(content="✅ Saved.")
            await asyncio.sleep(2)
            await interaction.delete_original_response()
        except Exception as e:
            print("[WARN] Could not clean up original response:", e)


@bot.tree.command(name="library", description="Search your Plex library for albums by artist or album name")
@app_commands.describe(query="Artist or album name to search for")
async def library(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)

    if not config.PLEX_TOKEN or not config.PLEX_URL:
        await interaction.followup.send("❌ Plex integration is not configured.")
        return

    headers = {"X-Plex-Token": config.PLEX_TOKEN}
    sanitized_query = sanitize_query(query)
    search_url = f"{config.PLEX_URL}/library/search?query={sanitized_query}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, headers=headers) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Failed to query Plex.")
                    return
                xml = await response.text()

        root = ET.fromstring(xml)
        results = []

        for elem in root.findall(".//Directory") + root.findall(".//Video"):
            if elem.attrib.get("type") != "album":
                continue

            title = elem.attrib.get("title", "")
            artist = elem.attrib.get("parentTitle", "")
            thumb = elem.attrib.get("thumb", "")
            guid_raw = elem.attrib.get("guid", "")

            combined = f"{artist} {title}"
            score = fuzz.token_sort_ratio(query.lower(), combined.lower())

            match = re.search(r"plex://album/([^?]+)", guid_raw)
            if match:
                guid = match.group(1)
                plexamp_url = f"https://listen.plex.tv/album/{guid}?source={config.PLEX_MACHINE_ID}"
                results.append({
                    "title": title,
                    "artist": artist,
                    "thumb": thumb,
                    "url": plexamp_url,
                    "score": score
                })

        if not results:
            await interaction.followup.send("❌ No albums found.")
            return

        # Sort by fuzzy score, then show top 5
        top_matches = sorted(results, key=lambda r: r["score"], reverse=True)[:5]

        for r in top_matches:
            embed = discord.Embed(
                title=r["title"],
                description=f"**Artist:** {r['artist']}",
                color=discord.Color.blue()
            )
            if r["thumb"]:
                embed.set_thumbnail(url=f"{config.PLEX_URL}{r['thumb']}?X-Plex-Token={config.PLEX_TOKEN}")

            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Plexamp", url=r["url"]))
            await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        print("[ERROR /library]", e)
        await interaction.followup.send("❌ Error occurred while searching your Plex library.")


@bot.tree.command(name="ripbulk", description="Batch download albums from streaming URLs or a .txt file")
@app_commands.describe(links="Paste multiple streaming links separated by spaces or newlines", file="Optional .txt file with links")
async def ripbulk(interaction: discord.Interaction, links: str = None, file: discord.Attachment = None):
    await interaction.response.defer(thinking=True)
    urls = []

    if file and file.filename.endswith(".txt"):
        try:
            content = await file.read()
            lines = content.decode().splitlines()
            urls.extend([line.strip() for line in lines if line.strip()])
        except Exception:
            await interaction.followup.send("❌ Failed to read the uploaded file.")
            return

    if links:
        urls.extend([u.strip() for u in links.splitlines() if u.strip()])

    if not urls:
        await interaction.followup.send("❌ No valid URLs provided.")
        return

    urls = list(set(urls))
    successful = []
    failed = []

    for url in urls:
        try:
            apple_url = url if "music.apple.com" in url else await fetch_apple_url(url)
            if not apple_url:
                failed.append(url)
                continue

            returncode, output = await run_gamdl(apple_url)
            if returncode == 0:
                successful.append(url)
                # Run beets import for each successful download
                await run_beet_import()
            else:
                failed.append(f"{url} (exit {returncode})")

        except Exception as e:
            logger.error("[ripbulk] %s", e)
            failed.append(url)

    embed = discord.Embed(
        title="📦 Bulk Download Summary",
        description=f"✅ **{len(successful)} succeeded**\n❌ **{len(failed)} failed**",
        color=discord.Color.green() if not failed else discord.Color.orange()
    )

    if failed:
        embed.add_field(name="Failed URLs", value="\n".join(failed[:5]) + ("\n..." if len(failed) > 5 else ""), inline=False)

    await interaction.followup.send(embed=embed)


class RecommendDropdown(discord.ui.View):
    def __init__(self, author_id, embed, url, guild):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.embed = embed
        self.guild = guild
        self.url = url
        self.message = None  # Will hold the prompt message so we can delete it

        music_town = discord.utils.get(guild.text_channels, name="music-town")
        members = []

        if music_town:
            for member in guild.members:
                perms = music_town.permissions_for(member)
                print(f"[DEBUG] Member: {member.display_name}, Bot: {member.bot}, CanRead: {perms.read_messages}")
                if not member.bot and perms.read_messages:
                    members.append(member)

        options = [
            discord.SelectOption(label=member.display_name, value=str(member.id))
            for member in members
        ][:25]

        if options:
            self.select = discord.ui.Select(
                placeholder="Choose people to recommend this album to...",
                options=options,
                min_values=1,
                max_values=len(options)  # Allow selecting multiple users
            )

            self.select.callback = self.select_callback
            self.add_item(self.select)
        else:
            print("[WARN] RecommendDropdown: No valid members found.")

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ You're not allowed to use this menu.", ephemeral=True)
            return

        selected_ids = [int(uid) for uid in self.select.values]
        confirmed_mentions = []

        for user_id in selected_ids:
            user = self.guild.get_member(user_id)
            if not user:
                continue

            rec_channel_name = f"listen-later-{user.name.lower().replace(' ', '-')}"
            rec_channel = discord.utils.get(self.guild.text_channels, name=rec_channel_name)

            if not rec_channel:
                author = self.guild.get_member(self.author_id)
                recipient = self.guild.get_member(user.id)

                overwrites = {
                    self.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    self.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                }

                if recipient:
                    overwrites[recipient] = discord.PermissionOverwrite(read_messages=True, send_messages=False)

                rec_channel = await self.guild.create_text_channel(rec_channel_name, overwrites=overwrites)

            # Build a fresh embed so streaming buttons are retained
            embed, view, _, _, _ = await build_album_embed(self.url)

            # Add "Recommended by" footer if recommender ≠ recipient
            if user.id != self.author_id:
                embed.set_footer(text=f"💡 Recommended by {interaction.user.display_name}")

            await post_album_message(
                rec_channel,
                embed,
                self.url,
                user.id,
                embed.title or "Unknown Album",
                "Unknown Artist",
                links=None,
                view=view,
                extra_reactions=["📥", "🎧", "🔁", "❌"]
            )

            confirmed_mentions.append(user.mention)

        # Show ephemeral confirmation to recommender
        if confirmed_mentions:
            try:
                await interaction.response.send_message(
                    f"✅ Recommended to {', '.join(confirmed_mentions)}", ephemeral=True
                )
            except discord.errors.NotFound:
                print("[WARN] RecommendDropdown interaction expired — skipping confirmation message.")
        else:
            await interaction.response.send_message("❌ No valid users were selected.", ephemeral=True)

        # 🧹 Clean up prompt and dropdown view
        try:
            if self.message:
                await self.message.delete()
        except Exception as e:
            print("[WARN] Could not delete recommend prompt:", e)

        try:
            await interaction.message.edit(content="✅ Recommendation sent.", view=None)
        except Exception as e:
            print("[WARN] Could not clear dropdown view:", e)

        self.stop()


class FakeInteraction:
    def __init__(self, message, user=None, artist=None, album=None, channel=None, url=None):
        self.message = message
        self.user = user or message.author
        self.channel = channel or message.channel
        self.guild = message.guild
        self.extras = {"artist": artist, "album": album, "url": url}

    async def defer(self, thinking=False, ephemeral=False):
        pass

    @property
    def followup(self):
        return self

    async def send(self, content=None, embed=None, file=None, view=None):
        try:
            msg = await self.channel.send(content=content, embed=embed, file=file, view=view)
            return msg
        except Exception as e:
            print("[WARN FakeInteraction.send failed]:", e)

    @property
    def response(self):
        return self

    @property
    def url(self):
        return self._url


async def post_album_message(channel, embed, url, user_id, artist, album, links=None, view=None, extra_reactions=None, file=None):
    try:
        print(f"[DEBUG] Sending embed to #{channel.name}: {embed.title}")
        msg = await channel.send(embed=embed, view=view, file=file)

        # Save for future reference
        saved_embeds[msg.id] = {"url": url, "user_id": user_id, "artist": artist, "album": album}

        # Add reactions if specified
        if extra_reactions:
            for reaction in extra_reactions:
                await msg.add_reaction(reaction)

        return msg

    except Exception as e:
        print(f"[ERROR] post_album_message failed in #{channel.name}: {e}")
        return None

    
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name != "music-town":
        return

    url_match = re.search(r"(https?://\S+)", message.content)
    if not url_match:
        return

    url = url_match.group(1)
    print(f"[AUTO-SAVE] Detected link: {url}")

    # 🚫 Skip non-music domains
    ignored_domains = [
        "tenor.com", "giphy.com", "imgur.com", "youtube.com", "youtu.be",
        "twitter.com", "x.com", "reddit.com", "tiktok.com"
    ]
    if any(domain in url for domain in ignored_domains):
        print("[AUTO-SAVE] Skipping non-music-related link.")
        return

    fake_interaction = FakeInteraction(message=message, user=message.author)

    try:
        print("[AUTO-SAVE] Calling handle_save_logic...")
        await handle_save_logic(fake_interaction, url, mirror_only=True)
        print("[AUTO-SAVE] handle_save_logic completed.")
        await message.delete()  # Optional
    except Exception as e:
        print(f"[AUTO-SAVE ERROR]: {type(e).__name__}: {e}")


async def handle_save_logic(interaction, url: str, mirror_only=False):
    try:
        user = interaction.user
        guild = interaction.guild
        
        # Fetch metadata early so we get artist/album
        metadata = await extract_metadata(url)
        if not metadata:
            logger.warning("Metadata extraction failed for URL: %s", url)
            return
            
        artist = metadata.get("artist", "Unknown Artist")
        album = metadata.get("album", "Unknown Album")
        
        if not artist or artist == "Unknown Artist" or not album or album == "Unknown Album":
            logger.warning("Incomplete metadata for URL %s: artist=%s, album=%s", url, artist, album)
            return

        plex_url = await get_plex_album_guid(artist, album)
        download_available = not plex_url

        # Now pass the resolved Plex URL
        embed, view, artist, album, links = await build_album_embed(url, plex_url=plex_url)

        if mirror_only:
            # === Auto-save or 📌 reaction → post to #music-town ===
            music_town = discord.utils.get(guild.text_channels, name="music-town")
            if music_town:
                reactions = []
                if download_available:
                    reactions.append("📥")
                reactions.extend(["📌", "🔁", "❌"])
                await post_album_message(music_town, embed, url, user.id, artist, album, links=links, view=view, extra_reactions=reactions)

        else:
            # === /save or manual 📌 → post to user's private listen-later channel ===
            channel_name = f"listen-later-{user.name.lower().replace(' ', '-')}"

            private = discord.utils.get(guild.text_channels, name=channel_name)
            if not private:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    user: discord.PermissionOverwrite(read_messages=True, send_messages=False),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                }
                private = await guild.create_text_channel(channel_name, overwrites=overwrites)
                print(f"[INFO] Created new channel: {channel_name}")

            reactions = []
            if download_available:
                reactions.append("📥")
            reactions.extend(["🎧", "🔁", "❌"])
            await post_album_message(private, embed, url, user.id, artist, album, links=links, view=view, extra_reactions=reactions)

    except Exception as e:
        print("[ERROR] handle_save_logic failed:", e)
        try:
            await interaction.followup.send("❌ Failed to save the album.", ephemeral=True)
        except:
            pass


async def post_hotupdates(channel: discord.TextChannel) -> bool:
    """Post trending albums to the given channel."""
    aoty, pitchfork, quietus, brooklyn, bandcamp = await asyncio.gather(
        fetch_aoty_trending(10),
        fetch_pitchfork_best_new(3),
        fetch_quietus_aotw(3),
        fetch_brooklynvegan_notable(),
        fetch_bandcamp_aotd(7),
    )

    if not aoty and not bandcamp and not pitchfork and not brooklyn and not quietus:
        return False

    header = datetime.datetime.now().strftime("%A, %m - %d")
    await channel.send(f"**{header}**")

    lists = [aoty, pitchfork, quietus, brooklyn, bandcamp]
    for album in [al for sub in lists for al in sub]:
        try:
            meta = await extract_metadata(album["url"], album.get("artist"), album.get("album"))
            album["links"] = meta.get("links", {})
            if meta.get("cover_url"):
                album["cover"] = meta["cover_url"]
        except Exception as e:
            print("[WARN hotupdates links]", e)
            album["links"] = {}

    sources = [
        ("Album of the Year", aoty),
        ("Pitchfork", pitchfork),
        ("The Quietus", quietus),
        ("BrooklynVegan", brooklyn),
        ("Bandcamp", bandcamp),
    ]

    source_colors = {
        "Album of the Year": discord.Color.light_grey(),
        "Pitchfork": discord.Color.from_rgb(228, 5, 3),
        "Bandcamp": discord.Color.from_rgb(0, 171, 189),
        "BrooklynVegan": discord.Color.from_rgb(0, 255, 0),
        "The Quietus": discord.Color.from_rgb(200, 150, 0),
    }

    for name, albums in sources:
        if not albums:
            continue
        lines = []
        for i, album in enumerate(albums, 1):
            line = f"{i}. **{album['artist']}** – **[{album['album']}]({album['url']})**"
            lines.append(line)
            extras = []
            if "appleMusic" in album.get("links", {}):
                extras.append(f"[Apple Music]({album['links']['appleMusic']['url']})")
            if "bandcamp" in album.get("links", {}):
                extras.append(f"[Bandcamp]({album['links']['bandcamp']['url']})")
            if extras:
                lines.append(" / ".join(extras))

        embed = discord.Embed(
            title=name,
            description="\n".join(lines),
            color=source_colors.get(name, discord.Color.orange()),
        )
        embed.set_author(name="🔥 Hot Updates")

        collage = await create_collage([a.get("cover") for a in albums if a.get("cover")])
        file = None
        if collage:
            embed.set_thumbnail(url="attachment://collage.jpg")
            file = discord.File(collage, filename="collage.jpg")

        await channel.send(embed=embed, file=file)

    return True


@bot.tree.command(name="hotupdates", description="Post trending albums to #hot-updates")
async def hotupdates(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)

    channel = discord.utils.get(interaction.guild.text_channels, name="hot-updates")
    if not channel:
        await interaction.followup.send("❌ Channel #hot-updates not found.", ephemeral=True)
        return

    success = await post_hotupdates(channel)
    if success:
        await interaction.followup.send("✅ Posted hot updates.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Failed to fetch trending albums.", ephemeral=True)


@bot.tree.command(name="import", description="Manually import files from downloads folder")
@app_commands.describe(
    autotag="Enable auto-tagging to fix metadata (default: False)",
    path="Custom path to import from (default: /downloads)"
)
async def manual_import(interaction: discord.Interaction, autotag: bool = False, path: str = None):
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    import_path = path or config.DOWNLOADS_FOLDER
    
    # Check if path exists and has files
    if not os.path.exists(import_path):
        await interaction.followup.send(f"❌ Path `{import_path}` does not exist.")
        return
        
    # Count files to import
    file_count = 0
    for root, dirs, files in os.walk(import_path):
        file_count += len([f for f in files if f.lower().endswith(('.mp3', '.flac', '.m4a', '.ogg', '.wma'))])
    
    if file_count == 0:
        await interaction.followup.send(f"❌ No music files found in `{import_path}`.")
        return
    
    # Start import
    mode_text = "with auto-tagging enabled" if autotag else "without auto-tagging (-A flag)"
    await interaction.followup.send(f"📀 Importing {file_count} music files from `{import_path}` {mode_text}...")
    
    success = await run_beet_import(use_autotag=autotag, path=import_path)
    
    if success:
        result_text = "✅ Import completed successfully!"
        if autotag:
            result_text += "\n🏷️ Files were processed with auto-tagging to improve metadata."
        else:
            result_text += "\n📁 Files were moved as-is without metadata changes."
        await interaction.followup.send(result_text)
    else:
        await interaction.followup.send("❌ Import failed. Check the logs for details.")


@bot.tree.command(name="sync", description="Sync slash commands to this server")
@commands.is_owner()
async def sync_commands(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    bot.tree.copy_global_to(guild=config.DEV_GUILD)
    await bot.tree.sync(guild=config.DEV_GUILD)
    await interaction.followup.send("✅ Commands synced.", ephemeral=True)


@tasks.loop(time=datetime.time(hour=17, minute=30))
async def scheduled_hotupdates():
    now = datetime.datetime.now()
    if now.weekday() in (2, 4):  # Wednesday and Friday
        guild = bot.get_guild(config.DEV_GUILD_ID)
        if guild:
            channel = discord.utils.get(guild.text_channels, name="hot-updates")
            if channel:
                await post_hotupdates(channel)


@scheduled_hotupdates.before_loop
async def before_scheduled_hotupdates():
    await bot.wait_until_ready()


# ==================== LISTENING ROOM COMMANDS ====================

@bot.tree.command(name="golive", description="Start a listening room for an album")
@app_commands.describe(
    source="Album name to search in your library OR Apple Music URL",
    album_name="Specific album name if searching library",
silent="Create room silently without public announcement (default: False)"
)
async def golive(interaction: discord.Interaction, source: str, album_name: str = None, silent: bool = False):
    """Start a listening room with an album."""
    await interaction.response.defer(thinking=True)
    
    try:
        # Check if user is already in a room
        current_room = room_manager.get_user_room(interaction.user.id)
        if current_room:
            await interaction.followup.send(f"❌ You're already in listening room `{current_room.room_id}`. Leave it first with `/leave`.")
            return
        
        # Determine source type and get metadata
        if "music.apple.com" in source or "spotify.com" in source:
            # Apple Music or Spotify URL
            try:
                if "spotify.com" in source:
                    # Convert Spotify to Apple Music
                    apple_url = await fetch_apple_url(source)
                    if not apple_url:
                        await interaction.followup.send("❌ Could not convert Spotify URL to Apple Music.")
                        return
                    source = apple_url
                
                metadata = await extract_metadata(source)
                artist = metadata.get("artist", "Unknown Artist")
                album = metadata.get("album", "Unknown Album")
                source_type = "apple_music"
                source_data = source
                
            except Exception as e:
                logger.error(f"Failed to extract metadata from URL: {e}")
                await interaction.followup.send("❌ Failed to get album information from that URL.")
                return
                
        else:
            # Search local library
            if not config.PLEX_TOKEN or not config.PLEX_URL:
                await interaction.followup.send("❌ Plex integration not configured for local library search.")
                return
            
            search_query = f"{source} {album_name}" if album_name else source
            
            # Search Plex for the album
            headers = {"X-Plex-Token": config.PLEX_TOKEN}
            sanitized_query = sanitize_query(search_query)
            search_url = f"{config.PLEX_URL}/library/search?query={sanitized_query}"
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(search_url, headers=headers) as response:
                        if response.status != 200:
                            await interaction.followup.send("❌ Failed to search your music library.")
                            return
                        xml = await response.text()
                
                root = ET.fromstring(xml)
                albums = []
                
                for elem in root.findall(".//Directory") + root.findall(".//Video"):
                    if elem.attrib.get("type") != "album":
                        continue
                    
                    title = elem.attrib.get("title", "")
                    artist_name = elem.attrib.get("parentTitle", "")
                    key = elem.attrib.get("key", "")
                    
                    albums.append({
                        "title": title,
                        "artist": artist_name,
                        "key": key,
                        "combined": f"{artist_name} - {title}"
                    })
                
                if not albums:
                    await interaction.followup.send(f"❌ No albums found for '{search_query}' in your library.")
                    return
                
                # Use the best match
                best_album = albums[0]
                artist = best_album["artist"]
                album = best_album["title"]
                source_type = "local"
                source_data = best_album["key"]
                
            except Exception as e:
                logger.error(f"Failed to search Plex library: {e}")
                await interaction.followup.send("❌ Failed to search your music library.")
                return
        
        # Create the listening room
        room = room_manager.create_room(
            host=interaction.user,
            guild=interaction.guild,
            artist=artist,
            album=album,
            source_type=source_type,
            source_data=source_data
        )
        
        # Create voice channel
        voice_channel_name = f"🎵 {album} - {interaction.user.display_name}"
        category = None  # You can set a specific category if you want
        
        try:
            room.voice_channel = await interaction.guild.create_voice_channel(
                name=voice_channel_name,
                category=category,
                user_limit=room.max_participants
            )
            
            # Create persistent text channel
            text_channel_name = f"💬-{album.lower().replace(' ', '-')}-chat"
            room.text_channel = await interaction.guild.create_text_channel(
                name=text_channel_name,
                category=category,
                topic=f"Chat for listening to {artist} - {album} | Room ID: {room.room_id}"
            )
            
        except Exception as e:
            logger.error(f"Failed to create channels: {e}")
            room_manager.cleanup_room(room.room_id)
            await interaction.followup.send("❌ Failed to create voice/text channels.")
            return
        
        # Create room announcement embed
        embed = discord.Embed(
            title=f"🎵 Listening Room Created",
            description=f"**{artist} - {album}**\n\nRoom ID: `{room.room_id}`\n\nAnyone can join and control the music!",
            color=discord.Color.purple()
        )
        embed.add_field(
            name="🎧 Voice Channel", 
            value=room.voice_channel.mention, 
            inline=True
        )
        embed.add_field(
            name="💬 Chat Channel", 
            value=room.text_channel.mention, 
            inline=True
        )
        embed.add_field(
            name="👥 Participants", 
            value=f"1/{room.max_participants}", 
            inline=True
        )
        embed.set_footer(text=f"Host: {interaction.user.display_name}")
        
        # Create join button
        view = discord.ui.View()
        join_button = discord.ui.Button(
            label="🎧 Join Room",
            style=discord.ButtonStyle.primary,
            custom_id=f"join_room:{room.room_id}"
        )
        view.add_item(join_button)
        
        # Send response based on silent mode
        if silent:
            # Silent mode: only creator sees the response
            await interaction.followup.send(
                f"🔇 **Silent room created!**\n\n"
                f"**{artist} - {album}**\n"
                f"Room ID: `{room.room_id}`\n"
                f"Voice: {room.voice_channel.mention}\n"
                f"Chat: {room.text_channel.mention}\n\n"
                f"Share the room ID or channels privately!",
                ephemeral=True
            )
        else:
            # Public mode: everyone can see, but no pings/notifications
            await interaction.followup.send(embed=embed, view=view, suppress_embeds=False, silent=True)
        
        # Send initial message to the text channel
        welcome_msg = f"🎵 **Welcome to the listening room for {artist} - {album}!**\n\n"
        welcome_msg += f"• Join the voice channel: {room.voice_channel.mention}\n"
        welcome_msg += f"• Anyone can control the music using reactions or commands\n"
        welcome_msg += f"• Room ID: `{room.room_id}`"
        
        await room.text_channel.send(welcome_msg)
        
        logger.info(f"Successfully created listening room {room.room_id} for {artist} - {album}")
        
    except Exception as e:
        logger.error(f"Error in /golive command: {e}")
        await interaction.followup.send("❌ An error occurred while creating the listening room.")


@bot.tree.command(name="join", description="Join a listening room")
@app_commands.describe(room_id="Room ID to join")
async def join_room_command(interaction: discord.Interaction, room_id: str):
    """Join an existing listening room."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    # Check if user is already in a room
    current_room = room_manager.get_user_room(interaction.user.id)
    if current_room:
        await interaction.followup.send(f"❌ You're already in room `{current_room.room_id}`. Leave it first with `/leave`.")
        return
    
    # Try to join the room
    success = room_manager.join_room(room_id, interaction.user)
    if not success:
        room = room_manager.get_room(room_id)
        if not room:
            await interaction.followup.send(f"❌ Room `{room_id}` not found.")
        elif room.is_full:
            await interaction.followup.send(f"❌ Room `{room_id}` is full ({room.max_participants}/{room.max_participants}).")
        else:
            await interaction.followup.send(f"❌ Failed to join room `{room_id}`.")
        return
    
    room = room_manager.get_room(room_id)
    await interaction.followup.send(f"✅ Joined listening room for **{room.artist} - {room.album}**!\n\nVoice: {room.voice_channel.mention}\nChat: {room.text_channel.mention}")
    
    # Announce in the text channel
    if room.text_channel:
        await room.text_channel.send(f"👋 **{interaction.user.display_name}** joined the room! ({len(room.participants)}/{room.max_participants})")


@bot.tree.command(name="leave", description="Leave your current listening room")
async def leave_room_command(interaction: discord.Interaction):
    """Leave the current listening room."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    room_id = await room_manager.leave_room(interaction.user)
    if not room_id:
        await interaction.followup.send("❌ You're not in any listening room.")
        return
    
    await interaction.followup.send(f"✅ Left listening room `{room_id}`.")


@bot.tree.command(name="rooms", description="List all active listening rooms")
async def list_rooms(interaction: discord.Interaction):
    """List all active listening rooms."""
    await interaction.response.defer(thinking=True)
    
    rooms = room_manager.get_all_rooms()
    if not rooms:
        await interaction.followup.send("🔇 No active listening rooms right now.")
        return
    
    embed = discord.Embed(
        title="🎵 Active Listening Rooms",
        color=discord.Color.blue()
    )
    
    for room in rooms:
        status = "🎵 Playing" if room.is_playing else "⏸️ Paused"
        participants = ", ".join([p.display_name for p in room.participants])
        
        embed.add_field(
            name=f"`{room.room_id}` - {room.artist} - {room.album}",
            value=f"Host: {room.host.display_name}\n"
                  f"Participants ({len(room.participants)}/{room.max_participants}): {participants}\n"
                  f"Status: {status}\n"
                  f"Voice: {room.voice_channel.mention if room.voice_channel else 'None'}",
            inline=False
        )
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="cleanup", description="[Admin] Delete all listening rooms and channels")
@commands.is_owner()
async def cleanup_all_rooms_command(interaction: discord.Interaction):
    """Admin command to clean up all listening rooms."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    rooms_count = len(room_manager.get_all_rooms())
    if rooms_count == 0:
        await interaction.followup.send("🔇 No active listening rooms to clean up.")
        return
    
    await room_manager.cleanup_all_rooms()
    await interaction.followup.send(f"🧹 Cleaned up {rooms_count} listening rooms and their channels.")


# Handle button interactions for joining rooms
@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Handle button clicks and other interactions."""
    if interaction.type != discord.InteractionType.component:
        return
    
    if interaction.data.get("custom_id", "").startswith("join_room:"):
        room_id = interaction.data["custom_id"].split(":", 1)[1]
        
        # Check if user is already in a room
        current_room = room_manager.get_user_room(interaction.user.id)
        if current_room:
            await interaction.response.send_message(
                f"❌ You're already in room `{current_room.room_id}`. Leave it first with `/leave`.",
                ephemeral=True
            )
            return
        
        # Try to join the room
        success = room_manager.join_room(room_id, interaction.user)
        if not success:
            room = room_manager.get_room(room_id)
            if not room:
                await interaction.response.send_message(f"❌ Room `{room_id}` not found.", ephemeral=True)
            elif room.is_full:
                await interaction.response.send_message(f"❌ Room is full ({room.max_participants}/{room.max_participants}).", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Failed to join room.", ephemeral=True)
            return
        
        room = room_manager.get_room(room_id)
        await interaction.response.send_message(
            f"✅ Joined listening room for **{room.artist} - {room.album}**!\n\nVoice: {room.voice_channel.mention}\nChat: {room.text_channel.mention}",
            ephemeral=True
        )
        
        # Announce in the text channel
        if room.text_channel:
            await room.text_channel.send(f"👋 **{interaction.user.display_name}** joined the room! ({len(room.participants)}/{room.max_participants})")


# ==================== END LISTENING ROOM COMMANDS ====================


# Add missing functions for reaction handling
async def mark_as_listened(interaction, message):
    """Mark an album as listened"""
    try:
        embed = message.embeds[0]
        # Create a new embed with updated status
        new_embed = discord.Embed(
            title=embed.title,
            description=embed.description + "\n\n🎧 *Listened*",
            color=discord.Color.green()
        )
        if embed.thumbnail:
            new_embed.set_thumbnail(url=embed.thumbnail.url)
        
        await message.edit(embed=new_embed)
        
    except Exception as e:
        print(f"[ERROR] mark_as_listened failed: {e}")


async def recommend_album(interaction, message):
    """Show recommendation dropdown"""
    try:
        if not message.embeds:
            return
            
        embed = message.embeds[0]
        url = saved_embeds.get(message.id, {}).get("url", "")
        
        if not url:
            await interaction.channel.send("❌ Could not find album URL for recommendation.")
            return
            
        dropdown = RecommendDropdown(interaction.user.id, embed, url, interaction.guild)
        prompt_msg = await interaction.channel.send("🔁 **Who would you like to recommend this album to?**", view=dropdown)
        dropdown.message = prompt_msg
        
    except Exception as e:
        print(f"[ERROR] recommend_album failed: {e}")


if __name__ == "__main__":
    bot.run(config.TOKEN)