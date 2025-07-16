import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import asyncio
import aiohttp
from pathlib import Path

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

TOKEN = os.getenv("DISCORD_TOKEN")
MUSIC_FOLDER = "/music"
DOWNLOADS_FOLDER = "/downloads"
DEV_GUILD_ID = 1036854855844757505
DEV_GUILD = discord.Object(id=DEV_GUILD_ID)
PLEX_TOKEN = os.getenv("PLEX_TOKEN")
PLEX_URL = os.getenv("PLEX_URL")
PLEX_MACHINE_ID = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True  # Required for reading mentions in messages
intents.members = True  # ✅ add this line
saved_embeds = {}  # message_id: { "url": str, "user_id": int }
bot = commands.Bot(command_prefix="!", intents=intents)


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
            print("[ERROR handle_rip_logic metadata fallback]", e)
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


async def run_beet_import():
    """Import downloads using local beets installation."""
    try:
        # Run beets import command directly
        process = await asyncio.create_subprocess_exec(
            "beet", "import", "-A", DOWNLOADS_FOLDER,
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
    cookies_path: str = "/app/cookies.txt",
    codec: str = "aac-legacy",
    remux_mode: str = "mp4box",
    output_path: str = "/downloads"
) -> tuple[int, str]:
    """Run gamdl with fallback codec options"""
    codecs = ["alac", "aac-legacy", "aac", "aac-he-legacy", "aac-he"]
    
    for codec in codecs:
        logger.info(f"🎧 Trying codec: {codec}")
        
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
            logger.info(f"✅ Download succeeded with codec: {codec}")
            return process.returncode, output
        else:
            logger.warning(f"❌ Failed with codec {codec}: {process.returncode}")
    
    logger.error("❌ All codec attempts failed")
    return 1, "All codec attempts failed"


async def download_album(interaction, url):
    artist = "Unknown Artist"
    album = "Unknown Album"

    try:
        metadata = await extract_metadata(url)
        artist = metadata["artist"]
        album = metadata["album"]
    except Exception as e:
        print(f"[WARN] Could not extract metadata from URL: {e}")

    # Send download message based on interaction type
    if hasattr(interaction, "response") and hasattr(interaction.response, "send_message"):
        await interaction.response.send_message(f"📥 Downloading **{artist} - {album}**...", ephemeral=True)
    else:
        await interaction.channel.send(f"📥 Downloading **{artist} - {album}**...")

    # Start download process with GAMDL
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


def get_latest_album_dir(base_path):
    try:
        dirs = [os.path.join(base_path, d) for d in os.listdir(base_path)]
        dirs = [d for d in dirs if os.path.isdir(d)]
        latest = max(dirs, key=os.path.getctime)
        return latest
    except Exception:
        return None


def extract_metadata_from_path(path):
    parts = Path(path).parts
    # expected: /downloads/Artist/Album
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    return "Unknown Artist", Path(path).name


def extract_cover_art_thumbnail(path):
    try:
        for root, _, files in os.walk(path):
            for file in files:
                if file.lower().endswith((".jpg", ".jpeg", ".png")) and "cover" in file.lower():
                    return os.path.join(root, file)
        return None
    except:
        return None


async def wait_for_album_in_music(artist, album_title, timeout=60):
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
    dirs = sorted(base_path.rglob("*"), key=os.path.getmtime, reverse=True)
    for d in dirs:
        if d.is_dir() and any(f.suffix in [".flac", ".mp3"] for f in d.glob("*")):
            return d
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
    if not PLEX_TOKEN or not PLEX_URL:
        return None

    headers = {"X-Plex-Token": PLEX_TOKEN}
    query = album

    try:
        async with aiohttp.ClientSession() as session:
            search_url = f"{PLEX_URL}/library/search?query={query}"
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
            if PLEX_MACHINE_ID:
                return f"https://listen.plex.tv/album/{best_guid}?source={PLEX_MACHINE_ID}"
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
            response = requests.get(cover)
            if response.ok:
                dominant = ColorThief(BytesIO(response.content)).get_color(quality=1)
                embed_color = discord.Color.from_rgb(*dominant)
    except Exception as e:
        print("[WARN] Could not extract color:", e)

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
                img_match = re.search(r