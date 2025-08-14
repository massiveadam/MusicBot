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
import tempfile
import shutil
import hashlib
import json
import logging

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
import urllib.parse

try:
    import pylast
    PYLAST_AVAILABLE = True
    logger = logging.getLogger(__name__)
    logger.info(f"pylast library loaded successfully: {pylast.__version__}")
except ImportError as e:
    PYLAST_AVAILABLE = False
    pylast = None
    logger = logging.getLogger(__name__)
    logger.warning(f"pylast library not available: {e}")

# Import our new modules
from config_constants import BotConstants
from utils import (
    validate_url, sanitize_filename, sanitize_query, normalize_text,
    is_ignored_domain, create_temp_directory, cleanup_temp_directory,
    extract_metadata_from_path, find_cover_art, count_music_files,
    format_duration, format_file_size, truncate_text,
    parse_discord_mention, validate_discord_id, safe_int, safe_float,
    chunk_list, remove_duplicates_preserve_order,
    is_valid_audio_file, is_valid_image_file
)


# Configuration management
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
        self.DOWNLOAD_TIMEOUT = BotConstants.DOWNLOAD_TIMEOUT
        self.RETRY_ATTEMPTS = BotConstants.RETRY_ATTEMPTS
        self.HIGH_QUALITY_AUDIO = os.getenv("HIGH_QUALITY_AUDIO", "true").lower() == "true"
        self.AUDIO_BITRATE = BotConstants.AUDIO_BITRATE_BPS // 1000  # Convert to kbps
        
        # Scrobbling configuration
        self.LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
        self.LASTFM_API_SECRET = os.getenv("LASTFM_API_SECRET")
        
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


# Audio Source Classes
class AudioTrack:
    """Represents a single audio track."""
    
    def __init__(self, title: str, artist: str, file_path: str, duration: int = 0, track_number: int | None = None, disc_number: int | None = None):
        self.title = title
        self.artist = artist
        self.file_path = file_path  # Local file path or temp file for downloaded tracks
        self.duration = duration  # Duration in seconds
        self.track_number = track_number
        self.disc_number = disc_number
        
    def __str__(self):
        return f"{self.artist} - {self.title}"


class AudioSource:
    """Handles audio source preparation for different input types."""
    
    @staticmethod
    async def prepare_local_album(plex_key: str) -> List[AudioTrack]:
        """Prepare tracks from local Plex library."""
        if not config.PLEX_TOKEN or not config.PLEX_URL:
            return []
            
        headers = {"X-Plex-Token": config.PLEX_TOKEN}
        album_url = f"{config.PLEX_URL}{plex_key}"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(album_url, headers=headers) as response:
                    if response.status != 200:
                        return []
                    xml = await response.text()
            
            root = ET.fromstring(xml)
            tracks = []
            
            for track in root.findall(".//Track"):
                title = track.attrib.get("title", "Unknown")
                artist = track.attrib.get("grandparentTitle", "Unknown Artist")
                duration = int(track.attrib.get("duration", "0")) // 1000  # Convert to seconds
                track_number = int(track.attrib.get("index", "0") or 0)
                disc_number = int(track.attrib.get("parentIndex", "0") or 0)
                
                # Get the highest quality media file path
                media_elements = track.findall(".//Media")
                best_media = None
                best_bitrate = 0
                
                # Find the highest bitrate media
                for media in media_elements:
                    bitrate = int(media.attrib.get("bitrate", "0"))
                    if bitrate > best_bitrate:
                        best_bitrate = bitrate
                        best_media = media
                
                if best_media is None and media_elements:
                    best_media = media_elements[0]  # Fallback to first media
                
                if best_media is not None:
                    part = best_media.find(".//Part")
                    if part is not None:
                        file_key = part.attrib.get("key", "")
                        # Request audio without forcing format - let Plex serve the original format
                        file_path = f"{config.PLEX_URL}{file_key}?X-Plex-Token={config.PLEX_TOKEN}"
                        
                        logger.info(f"Selected media with bitrate {best_bitrate}kbps for {title}")
                        tracks.append(AudioTrack(title, artist, file_path, duration, track_number, disc_number))
            
            # Sort by disc then track number, fallback to title
            tracks.sort(key=lambda t: (
                (t.disc_number if isinstance(t.disc_number, int) and t.disc_number > 0 else 1),
                (t.track_number if isinstance(t.track_number, int) and t.track_number > 0 else 9999),
                t.title.lower()
            ))
            return tracks
            
        except Exception as e:
            logger.error(f"Failed to prepare local album: {e}")
            return []
    
    @staticmethod
    async def prepare_apple_music_album(apple_url: str) -> List[AudioTrack]:
        """Download and prepare tracks from Apple Music."""
        temp_dir = None
        
        try:
            logger.info(f"Starting Apple Music download: {apple_url}")
            
            # Create temporary directory with proper error handling
            temp_dir = create_temp_directory(BotConstants.TEMP_DIR_PREFIX)
            logger.info(f"Created temp directory: {temp_dir}")
            
            # Download the album using gamdl with high quality and timeout
            logger.info("Starting GAMDL download...")
            returncode, output = await run_gamdl(apple_url, output_path=temp_dir, high_quality=config.HIGH_QUALITY_AUDIO)
            
            if returncode != 0:
                logger.error(f"Failed to download Apple Music album: {output}")
                return []
            
            # Find audio files in the temp directory
            tracks = []
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if is_valid_audio_file(file):
                        file_path = os.path.join(root, file)
                        
                        # Extract metadata from the file
                        try:
                            metadata = MutagenFile(file_path, easy=True)
                            if metadata:
                                title = metadata.get('title', [file])[0] if isinstance(metadata.get('title'), list) else metadata.get('title', file)
                                artist = metadata.get('artist', ['Unknown'])[0] if isinstance(metadata.get('artist'), list) else metadata.get('artist', 'Unknown')
                                duration = safe_int(getattr(metadata, 'length', 0))
                                
                                # Try to read track/disc numbers
                                track_number = None
                                disc_number = None
                                tn = metadata.get('tracknumber')
                                if isinstance(tn, list):
                                    tn = tn[0]
                                if isinstance(tn, str):
                                    track_number = safe_int(tn.split('/')[0])
                                
                                dn = metadata.get('discnumber')
                                if isinstance(dn, list):
                                    dn = dn[0]
                                if isinstance(dn, str):
                                    disc_number = safe_int(dn.split('/')[0])
                                
                                tracks.append(AudioTrack(title, artist, file_path, duration, track_number, disc_number))
                            else:
                                # Fallback to filename
                                tracks.append(AudioTrack(file, "Unknown Artist", file_path, 0))
                        except Exception as e:
                            logger.warning(f"Failed to read metadata from {file}: {e}")
                            tracks.append(AudioTrack(file, "Unknown Artist", file_path, 0))
            
            # Sort by disc then track number, fallback to title
            tracks.sort(key=lambda t: (
                (t.disc_number if isinstance(t.disc_number, int) and t.disc_number > 0 else 1),
                (t.track_number if isinstance(t.track_number, int) and t.track_number > 0 else 9999),
                t.title.lower()
            ))
            return tracks
            
        except Exception as e:
            logger.error(f"Failed to prepare Apple Music album: {e}")
            return []
        finally:
            # Clean up temp directory if we're not returning tracks (error case)
            if temp_dir and not tracks:
                cleanup_temp_directory(temp_dir)


# Listening Room Classes
class ListeningRoom:
    """Represents an active listening room session."""
    
    def __init__(self, host: discord.Member, guild: discord.Guild, artist: str, album: str, source_type: str, source_data: str):
        self.room_id = str(uuid.uuid4())[:BotConstants.ROOM_ID_LENGTH]  # Short ID for easy reference
        self.host = host
        self.guild = guild
        self.artist = artist
        self.album = album
        self.source_type = source_type  # 'local' or 'apple_music'
        self.source_data = source_data  # file path or URL
        
        # Channels
        self.voice_channel: Optional[discord.VoiceChannel] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.category: Optional[discord.CategoryChannel] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        
        # Participants
        self.participants: List[discord.Member] = [host]
        self.max_participants = BotConstants.MAX_ROOM_PARTICIPANTS
        
        # Playback state
        self.current_track = 0
        self.tracks: List[AudioTrack] = []  # List of AudioTrack objects
        self.is_playing = False
        self.is_paused = False
        self.start_time = None
        self.pause_time = None
        self._skipping = False  # Debounce flag for skip operations
        self._manual_stop_flag = False  # Flag to prevent auto-advance after manual stop
        self.temp_dir = None  # For Apple Music downloads
        self._connect_lock = asyncio.Lock()  # Lock for connection synchronization
        
        # Created timestamp
        self.created_at = time.time()
        
    @property
    def is_full(self) -> bool:
        return len(self.participants) >= self.max_participants
        
    @property
    def current_track_info(self) -> Optional[AudioTrack]:
        if 0 <= self.current_track < len(self.tracks):
            return self.tracks[self.current_track]
        return None
        
    def add_participant(self, member: discord.Member) -> bool:
        """Add a participant to the room. Returns True if successful."""
        if any(p.id == member.id for p in self.participants):
            return False
        if self.is_full:
            return False
        self.participants.append(member)
        return True
        
    def remove_participant(self, member: discord.Member) -> bool:
        """Remove a participant from the room. Returns True if successful."""
        for p in list(self.participants):
            if p.id == member.id:
                self.participants.remove(p)
                return True
        return False
        
    async def load_tracks(self) -> bool:
        """Load tracks based on source type."""
        try:
            logger.info(f"Loading tracks for room {self.room_id} (source_type: {self.source_type})")
            
            if self.source_type == "local":
                logger.info(f"Loading local album from: {self.source_data}")
                self.tracks = await AudioSource.prepare_local_album(self.source_data)
            elif self.source_type == "apple_music":
                logger.info(f"Loading Apple Music album from: {self.source_data}")
                self.tracks = await AudioSource.prepare_apple_music_album(self.source_data)
                # Store temp directory for cleanup
                if self.tracks and self.tracks[0].file_path:
                    self.temp_dir = os.path.dirname(self.tracks[0].file_path)
            else:
                logger.error(f"Unknown source type: {self.source_type}")
                return False
            
            logger.info(f"Successfully loaded {len(self.tracks)} tracks for room {self.room_id}")
            return len(self.tracks) > 0
            
        except asyncio.TimeoutError as e:
            logger.error(f"Timeout while loading tracks for room {self.room_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to load tracks for room {self.room_id}: {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    async def connect_voice(self) -> bool:
        """Connect to the voice channel with robust retry logic."""
        if not self.voice_channel:
            logger.error("No voice channel to connect to")
            return False
            
        try:
            # Serialize connects per room to avoid concurrent handshakes (can cause 4006)
            if hasattr(self, "_connecting") and self._connecting:
                # Wait briefly for existing connect to finish
                for _ in range(10):
                    await asyncio.sleep(0.3)
                    if not getattr(self, "_connecting", False):
                        break
            async with self._connect_lock:
                self._connecting = True
            # Check if already connected
            if self.voice_client and self.voice_client.is_connected():
                logger.info(f"Already connected to voice channel in room {self.room_id}")
                self._connecting = False
                return True
                
            logger.info(f"Connecting to voice channel: {self.voice_channel.name}")
            
            # Robust connection with manual retry strategy only (disable library auto-retry)
            max_attempts = BotConstants.VOICE_RETRY_ATTEMPTS
            base_delay = BotConstants.VOICE_BASE_DELAY
            # Small initial jitter helps avoid immediate 4006 after channel creation
            await asyncio.sleep(BotConstants.VOICE_STABILIZE_DELAY)
            
            for attempt in range(1, max_attempts + 1):
                try:
                    logger.info(f"Connection attempt {attempt}/{max_attempts}")
                    
                    # Strategy 1: Standard connect without library auto-retry (we manage retries)
                    if attempt <= 4:
                        self.voice_client = await self.voice_channel.connect(
                            reconnect=False,
                            timeout=BotConstants.VOICE_CONNECT_TIMEOUT,
                            self_deaf=True
                        )
                    else:
                        # Strategy 2: Hard reset, then connect fresh
                        if self.voice_client:
                            try:
                                await self.voice_client.disconnect(force=True)
                            except Exception:
                                pass
                            self.voice_client = None
                            await asyncio.sleep(1.0)
                        # Small jitter before retrying
                        await asyncio.sleep(0.5 * attempt)
                        self.voice_client = await self.voice_channel.connect(
                            reconnect=False,
                            timeout=BotConstants.VOICE_CONNECT_TIMEOUT * 1.5,  # Longer timeout for retry attempts
                            self_deaf=True
                        )
                    
                    # Wait for connection to stabilize
                    await asyncio.sleep(2.0)
                    
                    if self.voice_client and self.voice_client.is_connected():
                        logger.info(f"Successfully connected to voice channel in room {self.room_id}")
                        self._connecting = False
                        return True
                    else:
                        logger.warning(f"Voice client not connected after attempt {attempt}")
                        if self.voice_client:
                            await self.voice_client.disconnect()
                            self.voice_client = None
                        
                except discord.errors.ConnectionClosed as e:
                    code = getattr(e, 'code', None)
                    logger.error(f"Discord connection closed with code {code} (attempt {attempt}): {e}")
                    # 4006 session timeout: fully reset client and backoff
                    if code == 4006:
                        # On later attempts, recreate a fresh voice channel to avoid bad server affinity
                        if attempt >= 6 and self.voice_channel:
                            try:
                                old = self.voice_channel
                                new_name = f"{old.name}"
                                new_vc = await old.guild.create_voice_channel(
                                    name=new_name,
                                    category=old.category,
                                    user_limit=old.user_limit,
                                    bitrate=old.bitrate,
                                    overwrites=old.overwrites,
                                    reason="Recreate channel to recover from voice 4006"
                                )
                                # Swap
                                self.voice_channel = new_vc
                                try:
                                    await old.delete(reason="Replacing due to voice 4006")
                                except Exception:
                                    pass
                                # Give Discord a moment to propagate the new channel
                                await asyncio.sleep(2.0)
                            except Exception as ce:
                                logger.error(f"Failed to recreate voice channel: {ce}")
                        if self.voice_client:
                            try:
                                await self.voice_client.disconnect(force=True)
                            except Exception:
                                pass
                            self.voice_client = None
                        delay = min(BotConstants.VOICE_MAX_DELAY, base_delay * (2 ** (attempt - 1)))
                        logger.info(f"Waiting {delay}s before retry...")
                        await asyncio.sleep(delay)
                        continue
                    return False
                        
                except discord.errors.ClientException as e:
                    if "Already connected to a voice channel" in str(e):
                        logger.info("Already connected to voice channel")
                        return True
                    else:
                        logger.error(f"Discord client exception (attempt {attempt}): {e}")
                        if attempt == max_attempts:
                            return False
                        await asyncio.sleep(base_delay)
                        continue
                        
                except asyncio.TimeoutError:
                    logger.error(f"Voice connect timed out (attempt {attempt})")
                    if self.voice_client:
                        try:
                            await self.voice_client.disconnect(force=True)
                        except Exception:
                            pass
                        self.voice_client = None
                    await asyncio.sleep(base_delay * attempt)
                    continue

                except Exception as e:
                    logger.error(f"Unexpected error during voice connection (attempt {attempt}): {type(e).__name__}: {e}")
                    if attempt == max_attempts:
                        self._connecting = False
                        return False
                    await asyncio.sleep(base_delay)
                    continue
            
            logger.error(f"Failed to connect after {max_attempts} attempts")
            self._connecting = False
            return False
            
        except Exception as e:
            logger.error(f"Failed to connect to voice channel: {type(e).__name__}: {e}")
            self._connecting = False
            return False
    
    async def disconnect_voice(self):
        """Disconnect from voice channel."""
        if self.voice_client:
            await self.voice_client.disconnect()
            self.voice_client = None
    
    async def play_current_track(self) -> bool:
        """Start playing the current track with enhanced error handling."""
        if not self.current_track_info:
            logger.error("No current track to play")
            return False
            
        if not self.voice_client:
            logger.error("No voice client available")
            return False
            
        if not self.voice_client.is_connected():
            logger.error("Voice client not connected, cannot play audio")
            return False
            
        try:
            track = self.current_track_info
            logger.info(f"Attempting to play track: {track} (file: {track.file_path})")
            
            # Ensure any currently playing or paused audio is fully stopped and drained
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                logger.info("Stopping existing audio before starting new track...")
                self.voice_client.stop()
                await asyncio.sleep(0.5)
                # Drain and hard-kill lingering ffmpeg if present
                try:
                    player = getattr(self.voice_client, "_player", None)
                    proc = getattr(player, "_process", None) if player else None
                    if proc and proc.poll() is None:
                        logger.warning("FFmpeg process still running, terminating...")
                        proc.terminate()
                        # brief wait; then force kill if needed
                        await asyncio.sleep(0.5)
                        if proc.poll() is None:
                            logger.warning("FFmpeg process still alive, killing...")
                            proc.kill()
                except Exception as e:
                    logger.error(f"Error cleaning up FFmpeg process: {e}")
            
            # Set start time for tracking playback duration
            self.start_time = time.time()
            
            # Create FFmpeg audio source with simplified, proven options
            # Based on research of working Discord music bots
            ffmpeg_options = {
                'before_options': BotConstants.FFMPEG_BEFORE_OPTIONS,
                'options': BotConstants.FFMPEG_OPTIONS
            }
            
            # Alternative options if the simple approach fails
            alternative_options = {
                'before_options': BotConstants.FFMPEG_BEFORE_OPTIONS,
                'options': BotConstants.FFMPEG_OPUS_OPTIONS
            }
            
            if track.file_path.startswith("http"):
                # Streaming URL (Plex) - use simplified approach first
                logger.info(f"Playing streaming URL: {track.file_path[:50]}...")
                try:
                    # Try FFmpegOpusAudio first (Discord-native format)
                    source = discord.FFmpegOpusAudio(track.file_path, **ffmpeg_options)
                    logger.info(f"Successfully created FFmpegOpusAudio source with simple options")
                except Exception as e:
                    logger.warning(f"FFmpegOpusAudio failed, trying FFmpegPCMAudio: {e}")
                    try:
                        # Fallback to FFmpegPCMAudio with basic options
                        source = discord.FFmpegPCMAudio(track.file_path, **ffmpeg_options)
                        logger.info(f"Successfully created FFmpegPCMAudio source with simple options")
                    except Exception as e2:
                        logger.warning(f"Simple options failed, trying alternative: {e2}")
                        try:
                            # Last resort with explicit Opus encoding
                            source = discord.FFmpegOpusAudio(track.file_path, **alternative_options)
                            logger.info(f"Successfully created FFmpegOpusAudio source with alternative options")
                        except Exception as e3:
                            logger.error(f"All audio source creation methods failed: {e3}")
                            return False
            else:
                # Local file - use simplified approach
                logger.info(f"Playing local file: {track.file_path}")
                try:
                    source = discord.FFmpegOpusAudio(track.file_path, **ffmpeg_options)
                    logger.info(f"Successfully created FFmpegOpusAudio source for local file")
                except Exception as e:
                    logger.warning(f"FFmpegOpusAudio failed for local file, trying FFmpegPCMAudio: {e}")
                    try:
                        source = discord.FFmpegPCMAudio(track.file_path, **ffmpeg_options)
                        logger.info(f"Successfully created FFmpegPCMAudio source for local file")
                    except Exception as e2:
                        logger.error(f"Failed to create audio source for local file: {e2}")
                        return False
            
            # Play the audio with error callback
            def after_playing(error):
                if error:
                    logger.error(f"Audio playback error: {error}")
                    logger.error(f"Error type: {type(error).__name__}")
                    logger.error(f"Error details: {str(error)}")
                else:
                    logger.info(f"Track finished: {track}")
                # Schedule the track finished handler on the bot's event loop
                future = asyncio.run_coroutine_threadsafe(self._track_finished(error), bot.loop)
                try:
                    # Do not block; retrieve result opportunistically to surface exceptions in logs
                    future.result(timeout=0)
                except Exception:
                    pass
            
            # Additional validation before playing
            if not source:
                logger.error("FFmpeg source is None, cannot play")
                return False
                
            logger.info(f"Starting playback with FFmpeg source for track: {track}")
            self.voice_client.play(source, after=after_playing)
            self.is_playing = True
            self.is_paused = False
            self.pause_time = None
            
            # Update now playing for all scrobbling users
            await scrobble_manager.update_now_playing_for_room(self, track)
            
            # Update the UI to reflect the new track
            await self._update_ui()
            
            logger.info(f"Successfully started playing {track} in room {self.room_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to play track: {type(e).__name__}: {e}")
            return False
    
    async def _update_ui(self):
        """Update the UI with current track information."""
        try:
            # Find the original message with the now playing embed
            if hasattr(self, 'now_playing_message') and self.now_playing_message:
                try:
                    # Update the embed
                    updated_embed = await create_now_playing_embed(self)
                    
                    # Create new view with updated buttons
                    playback_controls = PlaybackControlView(self.room_id)
                    await playback_controls.update_buttons(self)
                    
                    # Edit the message with new embed and view
                    await self.now_playing_message.edit(
                        embed=updated_embed,
                        view=playback_controls
                    )
                    logger.info(f"Updated UI for room {self.room_id}")
                except discord.errors.NotFound:
                    logger.warning(f"Now playing message not found for room {self.room_id}")
                except Exception as e:
                    logger.error(f"Failed to update UI: {e}")
        except Exception as e:
            logger.error(f"Error in _update_ui: {e}")
    
    async def pause(self):
        """Pause playback."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            self.is_paused = True
            self.pause_time = time.time()
            logger.info(f"Paused playback in room {self.room_id}")
            await self._update_ui()
    
    async def resume(self):
        """Resume playback."""
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            self.is_paused = False
            # Adjust start time for pause duration
            if self.pause_time and self.start_time:
                pause_duration = time.time() - self.pause_time
                self.start_time += pause_duration
            self.pause_time = None
            logger.info(f"Resumed playback in room {self.room_id}")
            await self._update_ui()
    
    async def stop(self):
        """Stop playback with enhanced FFmpeg process management."""
        if self.voice_client:
            logger.info(f"Stopping playback in room {self.room_id}")
            # Mark that this stop is manual/intentional to avoid auto-advance from after-callback
            self._manual_stop_flag = True

            # Stop the audio
            self.voice_client.stop()
            self.is_playing = False
            self.is_paused = False
            self.pause_time = None
            
            # Brief settle, then ensure FFmpeg is terminated quickly (low-latency stop)
            await asyncio.sleep(0.5)
            if hasattr(self.voice_client, '_player') and self.voice_client._player:
                try:
                    proc = getattr(self.voice_client._player, '_process', None)
                    if proc and proc.poll() is None:
                        logger.warning("FFmpeg process still running, terminating...")
                        proc.terminate()
                        await asyncio.sleep(0.3)
                        if proc.poll() is None:
                            logger.warning("FFmpeg process still alive, killing...")
                            proc.kill()
                except Exception as e:
                    logger.error(f"Error managing FFmpeg process: {e}")
            
            logger.info(f"Stopped playback in room {self.room_id}")
            # Update UI after stopping
            await self._update_ui()
    
    async def skip_to_next(self) -> bool:
        """Skip to the next track with de-bounced logic to avoid double triggers."""
        if getattr(self, "_skipping", False):
            return False
        self._skipping = True
        try:
            if self.current_track < len(self.tracks) - 1:
                logger.info(f"Skipping to next track in room {self.room_id}")
                # Stop current playback and wait briefly for cleanup
                await self.stop()
                await asyncio.sleep(0.3)
                # Move to next track
                self.current_track += 1
                # Start playing the new track
                success = await self.play_current_track()
                if success and self.text_channel:
                    track = self.current_track_info
                    await self.text_channel.send(f"⏭️ Skipped to next track: **{track}**", allowed_mentions=discord.AllowedMentions.none())
                return success
            else:
                logger.info("Already at the last track")
                return False
        finally:
            self._skipping = False
    
    async def skip_to_previous(self) -> bool:
        """Skip to the previous track with de-bounced logic to avoid double triggers."""
        if getattr(self, "_skipping", False):
            return False
        self._skipping = True
        try:
            if self.current_track > 0:
                logger.info(f"Skipping to previous track in room {self.room_id}")
                await self.stop()
                await asyncio.sleep(0.3)
                self.current_track -= 1
                success = await self.play_current_track()
                if success and self.text_channel:
                    track = self.current_track_info
                    await self.text_channel.send(f"⏮️ Skipped to previous track: **{track}**", allowed_mentions=discord.AllowedMentions.none())
                return success
            else:
                logger.info("Already at the first track")
                return False
        finally:
            self._skipping = False
    
    async def _track_finished(self, error):
        """Called when a track finishes playing."""
        if error:
            logger.error(f"Audio playback error: {error}")
        else:
            # If we stopped manually (e.g., for skip), do not auto-advance
            if getattr(self, "_manual_stop_flag", False):
                self._manual_stop_flag = False
                return
            # Scrobble the completed track for all participants
            if self.current_track_info:
                # Check if track played for at least minimum time or half its duration (Last.fm requirement)
                play_duration = time.time() - self.start_time if hasattr(self, 'start_time') else 0
                track_duration = self.current_track_info.duration or 240  # Default 4 minutes if unknown
                min_scrobble_time = min(BotConstants.MIN_SCROBBLE_TIME, track_duration * BotConstants.SCROBBLE_PERCENTAGE)
                
                if play_duration >= min_scrobble_time:
                    scrobbled_users = await scrobble_manager.scrobble_for_room_participants(self, self.current_track_info)
                    if scrobbled_users and self.text_channel:
                        await self.text_channel.send(f"🎵 Scrobbled to Last.fm for: {', '.join(scrobbled_users)}", allowed_mentions=discord.AllowedMentions.none())
                else:
                    if self.text_channel:
                        remaining = int(min_scrobble_time - play_duration)
                        if remaining > 0:
                            await self.text_channel.send(
                                f"⌛ Not scrobbled (played {int(play_duration)}s, need {int(min_scrobble_time)}s)"
                            )
        
        # Auto-advance to next track (respect skip debounce)
        if not await self.skip_to_next():
            # End of album
            self.is_playing = False
            if self.text_channel:
                await self.text_channel.send("🎵 **Album finished!** Thanks for listening together! 🎉", allowed_mentions=discord.AllowedMentions.none())
                # Update the UI to show stopped state
                await self._update_ui()
    
    async def cleanup(self):
        """Clean up room resources."""
        await self.disconnect_voice()
        
        # Clean up temp directory for Apple Music downloads
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                logger.info(f"Cleaned up temp directory: {self.temp_dir}")
            except Exception as e:
                logger.error(f"Failed to clean up temp directory: {e}")


class ScrobbleUser:
    """Represents a user's scrobbling configuration."""
    
    def __init__(self, discord_id: int, lastfm_username: str, session_key: str):
        self.discord_id = discord_id
        self.lastfm_username = lastfm_username
        self.session_key = session_key
        self.network = None
        self._setup_network()
    
    def _setup_network(self):
        """Setup Last.fm network connection."""
        if PYLAST_AVAILABLE and config.LASTFM_API_KEY and config.LASTFM_API_SECRET:
            self.network = pylast.LastFMNetwork(
                api_key=config.LASTFM_API_KEY,
                api_secret=config.LASTFM_API_SECRET,
                session_key=self.session_key
            )
    
    async def scrobble_track(self, track: 'AudioTrack', timestamp: int = None):
        """Scrobble a track for this user."""
        if not self.network:
            return False
        
        try:
            timestamp = timestamp or int(time.time())
            await asyncio.get_event_loop().run_in_executor(
                None,
                self.network.scrobble,
                track.artist,
                track.title,
                timestamp
            )
            logger.info(f"Scrobbled '{track.title}' by {track.artist} for user {self.lastfm_username}")
            return True
        except Exception as e:
            logger.error(f"Failed to scrobble for user {self.lastfm_username}: {e}")
            return False
    
    async def update_now_playing(self, track: 'AudioTrack'):
        """Update now playing status for this user."""
        if not self.network:
            return False
        
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                self.network.update_now_playing,
                track.artist,
                track.title
            )
            return True
        except Exception as e:
            logger.error(f"Failed to update now playing for user {self.lastfm_username}: {e}")
            return False


class ScrobbleManager:
    """Manages scrobbling for all users."""
    
    def __init__(self):
        self.users: Dict[int, ScrobbleUser] = {}  # discord_id -> ScrobbleUser
        # Persist inside /app/.beets (already a volume) unless overridden
        self.scrobble_data_file = os.getenv("SCROBBLE_DATA_FILE", "/app/.beets/scrobble_users.json")
        # Ensure parent directory exists
        try:
            os.makedirs(os.path.dirname(self.scrobble_data_file), exist_ok=True)
        except Exception:
            pass
        self._load_users()
    
    def _load_users(self):
        """Load saved user scrobbling data."""
        try:
            if os.path.exists(self.scrobble_data_file):
                with open(self.scrobble_data_file, 'r') as f:
                    data = json.load(f)
                    for discord_id_str, user_data in data.items():
                        discord_id = int(discord_id_str)
                        self.users[discord_id] = ScrobbleUser(
                            discord_id=discord_id,
                            lastfm_username=user_data['username'],
                            session_key=user_data['session_key']
                        )
                logger.info(f"Loaded {len(self.users)} scrobbling users")
        except Exception as e:
            logger.error(f"Failed to load scrobbling users: {e}")
    
    def _save_users(self):
        """Save user scrobbling data."""
        try:
            data = {}
            for discord_id, user in self.users.items():
                data[str(discord_id)] = {
                    'username': user.lastfm_username,
                    'session_key': user.session_key
                }
            with open(self.scrobble_data_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save scrobbling users: {e}")
    
    def add_user(self, discord_id: int, lastfm_username: str, session_key: str):
        """Add or update a user's scrobbling configuration."""
        self.users[discord_id] = ScrobbleUser(discord_id, lastfm_username, session_key)
        self._save_users()
        logger.info(f"Added scrobbling user: {lastfm_username} (Discord ID: {discord_id})")
    
    def remove_user(self, discord_id: int):
        """Remove a user's scrobbling configuration."""
        if discord_id in self.users:
            username = self.users[discord_id].lastfm_username
            del self.users[discord_id]
            self._save_users()
            logger.info(f"Removed scrobbling user: {username} (Discord ID: {discord_id})")
            return True
        return False
    
    def get_user(self, discord_id: int) -> Optional[ScrobbleUser]:
        """Get a user's scrobbling configuration."""
        return self.users.get(discord_id)
    
    async def scrobble_for_room_participants(self, room: 'ListeningRoom', track: 'AudioTrack'):
        """Scrobble a track for all participants in a room who have scrobbling enabled."""
        timestamp = int(time.time())
        scrobbled_users = []
        
        for participant in room.participants:
            user = self.get_user(getattr(participant, 'id', participant))
            if user:
                success = await user.scrobble_track(track, timestamp)
                if success:
                    scrobbled_users.append(user.lastfm_username)
        
        if scrobbled_users:
            logger.info(f"Scrobbled '{track.title}' for users: {', '.join(scrobbled_users)}")
        
        return scrobbled_users
    
    async def update_now_playing_for_room(self, room: 'ListeningRoom', track: 'AudioTrack'):
        """Update now playing for all participants in a room who have scrobbling enabled."""
        updated_users = []
        
        for participant in room.participants:
            user = self.get_user(getattr(participant, 'id', participant))
            if user:
                success = await user.update_now_playing(track)
                if success:
                    updated_users.append(user.lastfm_username)
        
        return updated_users
    
    async def get_auth_url(self) -> Optional[str]:
        """Get Last.fm authentication URL with proper token flow."""
        if not PYLAST_AVAILABLE or not config.LASTFM_API_KEY:
            return None
        
        try:
            # Use direct Last.fm API call instead of pylast methods
            import hashlib
            import aiohttp
            
            logger.info(f"Getting Last.fm auth token via API with key: {config.LASTFM_API_KEY[:8]}...")
            
            # Create API signature
            api_sig_string = f"api_key{config.LASTFM_API_KEY}methodauth.getToken{config.LASTFM_API_SECRET}"
            api_sig = hashlib.md5(api_sig_string.encode('utf-8')).hexdigest()
            
            # Make API request
            async with aiohttp.ClientSession() as session:
                params = {
                    'method': 'auth.getToken',
                    'api_key': config.LASTFM_API_KEY,
                    'api_sig': api_sig,
                    'format': 'json'
                }
                
                async with session.get('https://ws.audioscrobbler.com/2.0/', params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        if 'token' in data:
                            token = data['token']
                            logger.info(f"Got Last.fm token: {token[:8]}...")
                            return f"https://www.last.fm/api/auth/?api_key={config.LASTFM_API_KEY}&token={token}"
                        else:
                            logger.error(f"No token in Last.fm response: {data}")
                            return None
                    else:
                        logger.error(f"Last.fm API error: {response.status}")
                        return None
            
        except Exception as e:
            logger.error(f"Failed to get Last.fm auth token: {type(e).__name__}: {e}")
            # For debugging - let's also try a simpler approach
            logger.info("Falling back to basic auth URL without token...")
            return f"https://www.last.fm/api/auth/?api_key={config.LASTFM_API_KEY}"
    
    async def get_session_key(self, token: str) -> Optional[str]:
        """Exchange an authorized token for a session key."""
        if not PYLAST_AVAILABLE or not config.LASTFM_API_KEY:
            return None
        
        try:
            # Use direct Last.fm API call for session key exchange
            import hashlib
            import aiohttp
            
            logger.info(f"Exchanging token for session key: {token[:8]}...")
            
            # Create API signature for auth.getSession
            api_sig_string = f"api_key{config.LASTFM_API_KEY}methodauth.getSessiontoken{token}{config.LASTFM_API_SECRET}"
            api_sig = hashlib.md5(api_sig_string.encode('utf-8')).hexdigest()
            
            # Make API request
            async with aiohttp.ClientSession() as session:
                params = {
                    'method': 'auth.getSession',
                    'api_key': config.LASTFM_API_KEY,
                    'token': token,
                    'api_sig': api_sig,
                    'format': 'json'
                }
                
                async with session.get('https://ws.audioscrobbler.com/2.0/', params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        if 'session' in data and 'key' in data['session']:
                            session_key = data['session']['key']
                            logger.info(f"Got Last.fm session key: {session_key[:8]}...")
                            return session_key
                        else:
                            logger.error(f"No session key in Last.fm response: {data}")
                            return None
                    else:
                        logger.error(f"Last.fm session API error: {response.status}")
                        return None
            
        except Exception as e:
            logger.error(f"Failed to get Last.fm session key: {type(e).__name__}: {e}")
            return None


class ListeningRoomManager:
    """Manages all active listening rooms."""
    
    def __init__(self):
        self.rooms: Dict[str, ListeningRoom] = {}  # room_id -> ListeningRoom
        self.user_rooms: Dict[int, str] = {}  # user_id -> room_id
        self._global_voice_lock = asyncio.Lock()  # Serialize voice connections globally
        
    async def create_room(self, host: discord.Member, guild: discord.Guild, artist: str, album: str, source_type: str, source_data: str) -> ListeningRoom:
        """Create a new listening room."""
        # Remove user from any existing room first
        await self.leave_room(host)
        
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
        
    async def join_room(self, room_id: str, member: discord.Member) -> bool:
        """Add a user to a room. Returns True if successful."""
        room = self.get_room(room_id)
        if not room:
            return False
            
        # Remove from current room first
        await self.leave_room(member)
        
        if room.add_participant(member):
            self.user_rooms[member.id] = room_id
            logger.info(f"User {member.name} joined room {room_id}")
            return True
        return False
        
    async def leave_room(self, member: discord.Member) -> Optional[str]:
        """Remove a user from their current room. Returns room_id if they were in one."""
        current_room_id = self.user_rooms.get(member.id)
        if not current_room_id:
            # Also check for stale participant presence in any room and clean
            for rid, room in list(self.rooms.items()):
                if member in room.participants:
                    room.remove_participant(member)
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
            
            # Clean up room resources (audio, temp files, etc.)
            await room.cleanup()
            
            # Delete Discord channels
            try:
                if room.text_channel:
                    # Send farewell message before deleting (minimal notifications)
                    await room.text_channel.send("🎵 **Listening room ended.** Thanks for listening together! 👋", allowed_mentions=discord.AllowedMentions.none())
                    await asyncio.sleep(2)  # Give people time to see the message
                    await room.text_channel.delete(reason="Listening room ended")
                    logger.info(f"Deleted text channel for room {room_id}")
                    
                if room.voice_channel:
                    await room.voice_channel.delete(reason="Listening room ended")
                    logger.info(f"Deleted voice channel for room {room_id}")
                    
                if room.category:
                    await room.category.delete(reason="Listening room ended")
                    logger.info(f"Deleted category for room {room_id}")
                    
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


# Global managers
room_manager = ListeningRoomManager()
scrobble_manager = ScrobbleManager()


# Playback Control UI
class PlaybackControlView(discord.ui.View):
    """Interactive playback controls for listening rooms."""
    
    def __init__(self, room_id: str):
        super().__init__(timeout=None)  # Persistent view
        self.room_id = room_id
        
    async def update_buttons(self, room: ListeningRoom):
        """Update button states based on room status."""
        # Update play/pause button
        play_pause_button = self.children[0]
        if room.is_playing and not room.is_paused:
            play_pause_button.label = "⏸️ Pause"
            play_pause_button.style = discord.ButtonStyle.secondary
        else:
            play_pause_button.label = "▶️ Play"
            play_pause_button.style = discord.ButtonStyle.green
            
        # Update skip buttons based on track position
        prev_button = self.children[1]
        next_button = self.children[2]
        
        prev_button.disabled = room.current_track <= 0
        next_button.disabled = room.current_track >= len(room.tracks) - 1
    
    @discord.ui.button(label="▶️ Play", style=discord.ButtonStyle.green, custom_id="play_pause")
    async def play_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = room_manager.get_room(self.room_id)
        if not room:
            await interaction.response.send_message("❌ Room not found.", ephemeral=True)
            return
            
        if interaction.user not in room.participants:
            await interaction.response.send_message("❌ You're not in this listening room.", ephemeral=True)
            return
        
        try:
            if room.is_playing and not room.is_paused:
                # Pause
                await room.pause()
                action = "paused"
                emoji = "⏸️"
            else:
                # Play or resume
                if room.is_paused:
                    await room.resume()
                    action = "resumed"
                else:
                    await room.play_current_track()
                    action = "started"
                emoji = "▶️"
            
            # Update the UI
            await self.update_buttons(room)
            await interaction.response.edit_message(view=self)
            
            # Announce the action
            if room.text_channel:
                await room.text_channel.send(f"{emoji} **{interaction.user.display_name}** {action} the music", allowed_mentions=discord.AllowedMentions.none())
                
        except Exception as e:
            logger.error(f"Play/pause error: {e}")
            await interaction.response.send_message("❌ Failed to control playback.", ephemeral=True)
    
    @discord.ui.button(label="⏮️ Previous", style=discord.ButtonStyle.secondary, custom_id="previous")
    async def previous_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = room_manager.get_room(self.room_id)
        if not room:
            await interaction.response.send_message("❌ Room not found.", ephemeral=True)
            return
            
        if interaction.user not in room.participants:
            await interaction.response.send_message("❌ You're not in this listening room.", ephemeral=True)
            return
        
        if room.current_track <= 0:
            await interaction.response.send_message("❌ Already at the first track.", ephemeral=True)
            return
        
        try:
            success = await room.skip_to_previous()
            await self.update_buttons(room)
            await interaction.response.edit_message(view=self)
            
            if success and room.text_channel:
                track = room.current_track_info
                await room.text_channel.send(f"⏮️ **{interaction.user.display_name}** skipped to previous track: **{track}**", allowed_mentions=discord.AllowedMentions.none())
            
        except Exception as e:
            logger.error(f"Previous track error: {e}")
            await interaction.response.send_message("❌ Failed to skip to previous track.", ephemeral=True)
    
    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="next")
    async def next_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = room_manager.get_room(self.room_id)
        if not room:
            await interaction.response.send_message("❌ Room not found.", ephemeral=True)
            return
            
        if interaction.user not in room.participants:
            await interaction.response.send_message("❌ You're not in this listening room.", ephemeral=True)
            return
        
        if room.current_track >= len(room.tracks) - 1:
            await interaction.response.send_message("❌ Already at the last track.", ephemeral=True)
            return
        
        try:
            success = await room.skip_to_next()
            await self.update_buttons(room)
            await interaction.response.edit_message(view=self)
            
            if success and room.text_channel:
                track = room.current_track_info
                await room.text_channel.send(f"⏭️ **{interaction.user.display_name}** skipped to next track: **{track}**", allowed_mentions=discord.AllowedMentions.none())
            
        except Exception as e:
            logger.error(f"Next track error: {e}")
            await interaction.response.send_message("❌ Failed to skip to next track.", ephemeral=True)
    
    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop_playback(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = room_manager.get_room(self.room_id)
        if not room:
            await interaction.response.send_message("❌ Room not found.", ephemeral=True)
            return
            
        if interaction.user not in room.participants:
            await interaction.response.send_message("❌ You're not in this listening room.", ephemeral=True)
            return
        
        try:
            await room.stop()
            await self.update_buttons(room)
            await interaction.response.edit_message(view=self)
            
            if room.text_channel:
                await room.text_channel.send(f"⏹️ **{interaction.user.display_name}** stopped the music", allowed_mentions=discord.AllowedMentions.none())
            
        except Exception as e:
            logger.error(f"Stop playback error: {e}")
            await interaction.response.send_message("❌ Failed to stop playback.", ephemeral=True)

async def create_now_playing_embed(room: ListeningRoom) -> discord.Embed:
    """Create a 'Now Playing' embed for the room."""
    track = room.current_track_info
    if not track:
        embed = discord.Embed(
            title="🎵 Listening Room",
            description=f"**{room.artist} - {room.album}**\n\nNo track currently playing.",
            color=discord.Color.blue()
        )
    else:
        # Track position
        track_num = room.current_track + 1
        total_tracks = len(room.tracks)
        
        # Status
        if room.is_playing and not room.is_paused:
            status = "🎵 Now Playing"
            color = discord.Color.green()
        elif room.is_paused:
            status = "⏸️ Paused"
            color = discord.Color.orange()
        else:
            status = "⏹️ Stopped"
            color = discord.Color.red()
        
        embed = discord.Embed(
            title=status,
            description=f"**{track}**\n\nFrom: *{room.album}*",
            color=color
        )
        embed.add_field(name="Track", value=f"{track_num}/{total_tracks}", inline=True)
        embed.add_field(name="Participants", value=f"{len(room.participants)}/{room.max_participants}", inline=True)
        embed.add_field(name="Room ID", value=f"`{room.room_id}`", inline=True)
        
        if track.duration > 0:
            duration_str = f"{track.duration // 60}:{track.duration % 60:02d}"
            embed.add_field(name="Duration", value=duration_str, inline=True)
    
    return embed


intents = discord.Intents.default()
intents.message_content = True  # Required for reading mentions in messages
intents.members = True  # ✅ add this line
intents.voice_states = True  # Ensure voice state events are available
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
                logger.warning(f"fetch_apple_url request failed: {resp.status}")
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
    output_path: str = None,
    high_quality: bool = True
) -> tuple[int, str]:
    """Run gamdl with highest quality settings for listening rooms"""
    cookies_path = cookies_path or config.COOKIES_PATH
    output_path = output_path or config.DOWNLOADS_FOLDER
    
    # Use highest quality codec available
    if high_quality:
        # Try ALAC (lossless) first, fall back to high-quality AAC
        codec = "alac"
        logger.info("🎧 Using ALAC (lossless) codec for highest quality")
    else:
        codec = codec or config.GAMDL_CODEC
        logger.info(f"🎧 Using {codec} codec")
    
    cmd_args = [
        "gamdl",
        "--cookies-path", cookies_path,
        "--codec-song", codec,
        "--remux-mode", remux_mode,
        "--output-path", output_path,
    ]
    
    # Add quality settings for listening rooms
    if high_quality:
        cmd_args.extend([
            "--template-folder", "{album_artist}/{album}",  # Better organization
            "--template-file", "{track:02d} {title}",       # Track numbers
            "--exclude-tags", "false",                      # Keep all metadata
            "--sanity-check", "true",                       # Verify downloads
        ])
    
    cmd_args.append(url)
    
    try:
        # Add timeout to prevent hanging
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            ),
            timeout=BotConstants.DOWNLOAD_TIMEOUT
        )
        
        stdout, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=BotConstants.DOWNLOAD_TIMEOUT
        )
        output = stdout.decode("utf-8").strip()
        
        if process.returncode == 0:
            logger.info(f"✅ Download succeeded with {codec}")
        else:
            # If ALAC failed, try falling back to AAC
            if high_quality and codec == "alac":
                logger.warning(f"❌ ALAC failed, falling back to AAC-legacy")
                return await run_gamdl(url, cookies_path, "aac-legacy", remux_mode, output_path, False)
            else:
                logger.warning(f"❌ Failed with {codec}: {process.returncode}")
                logger.warning(f"Output: {output}")
        
        return process.returncode, output
        
    except asyncio.TimeoutError:
        logger.error(f"❌ GAMDL download timed out after {BotConstants.DOWNLOAD_TIMEOUT}s")
        # Try to kill the process if it's still running
        try:
            if process and process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
        except:
            pass
        return 1, f"Download timed out after {BotConstants.DOWNLOAD_TIMEOUT} seconds"
    except Exception as e:
        logger.error(f"❌ Unexpected error in GAMDL: {e}")
        return 1, f"Unexpected error: {str(e)}"


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
    music_town = discord.utils.get(interaction.guild.text_channels, name=BotConstants.MUSIC_TOWN_CHANNEL)
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
        logger.warning(f"get_plex_album_guid failed: {e}")
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

    logger.debug(f"Embed created: {artist} - {album}, source: {source_name}")
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
        logger.warning(f"fetch_aoty_trending failed: {e}")
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
        logger.warning(f"fetch_bandcamp_aotd failed: {e}")
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
        logger.warning(f"fetch_quietus_aotw failed: {e}")
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
        logger.warning(f"fetch_pitchfork_best_new failed: {e}")
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
        logger.warning(f"fetch_brooklynvegan_notable failed: {e}")
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
            logger.warning(f"create_collage fetch failed: {e}")
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
                logger.warning(f"create_collage open failed: {e}")

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
        logger.error(f"Could not fetch message: {e}")
        return

    emoji = str(payload.emoji)
    if message.id not in saved_embeds:
        return  # Not one of our tracked messages

    url = saved_embeds[message.id]["url"]

    # Create fake interaction with message + user (for download/save/etc)
    try:
        fake_interaction = FakeInteraction(message=message, user=member)
    except Exception as e:
        logger.error(f"Failed to create FakeInteraction: {e}")
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
        logger.info("Cleared existing guild commands")
        bot.tree.copy_global_to(guild=config.DEV_GUILD)
        await bot.tree.sync(guild=config.DEV_GUILD)
        logger.info(f"Synced commands to dev guild {config.DEV_GUILD_ID}")
    except Exception as e:
        logger.warning(f"Failed to sync commands: {e}")
    logger.info(f"Logged in as {bot.user}")
    logger.info(f"Using music folder: {config.MUSIC_FOLDER}")

    if config.PLEX_TOKEN and config.PLEX_URL:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{config.PLEX_URL}/?X-Plex-Token={config.PLEX_TOKEN}") as resp:
                    if resp.status == 200:
                        xml = await resp.text()
                        match = re.search(r'machineIdentifier="([^"]+)"', xml)
                        if match:
                            config.PLEX_MACHINE_ID = match.group(1)
                            logger.info(f"Found Plex machine ID: {config.PLEX_MACHINE_ID}")
        except Exception as e:
            logger.warning(f"Could not fetch Plex machine ID: {e}")

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
        logger.error(f"/save failed: {e}")
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
            logger.warning(f"Could not clean up original response: {e}")


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
            await interaction.followup.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

    except Exception as e:
        logger.error(f"/library failed: {e}")
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

        music_town = discord.utils.get(guild.text_channels, name=BotConstants.MUSIC_TOWN_CHANNEL)
        members = []

        if music_town:
            for member in guild.members:
                perms = music_town.permissions_for(member)
                logger.debug(f"Member: {member.display_name}, Bot: {member.bot}, CanRead: {perms.read_messages}")
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
            logger.warning("RecommendDropdown: No valid members found.")

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

            rec_channel_name = f"{BotConstants.LISTEN_LATER_PREFIX}{user.name.lower().replace(' ', '-')}"
            rec_channel = discord.utils.get(self.guild.text_channels, name=rec_channel_name)

            if not rec_channel:
                author = self.guild.get_member(self.author_id)
                recipient = self.guild.get_member(user.id)

                overwrites = {
                    self.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    self.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                }

                if recipient:
                    overwrites[recipient] = discord.PermissionOverwrite(view_channel=True, send_messages=False)

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
                logger.warning("RecommendDropdown interaction expired — skipping confirmation message.")
        else:
            await interaction.response.send_message("❌ No valid users were selected.", ephemeral=True)

        # 🧹 Clean up prompt and dropdown view
        try:
            if self.message:
                await self.message.delete()
        except Exception as e:
            logger.warning(f"Could not delete recommend prompt: {e}")

        try:
            await interaction.message.edit(content="✅ Recommendation sent.", view=None)
        except Exception as e:
            logger.warning(f"Could not clear dropdown view: {e}")

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
            logger.warning(f"FakeInteraction.send failed: {e}")

    @property
    def response(self):
        return self

    @property
    def url(self):
        return self._url


async def post_album_message(channel, embed, url, user_id, artist, album, links=None, view=None, extra_reactions=None, file=None):
    try:
        logger.debug(f"Sending embed to #{channel.name}: {embed.title}")
        msg = await channel.send(embed=embed, view=view, file=file)

        # Save for future reference
        saved_embeds[msg.id] = {"url": url, "user_id": user_id, "artist": artist, "album": album}

        # Add reactions if specified
        if extra_reactions:
            for reaction in extra_reactions:
                await msg.add_reaction(reaction)

        return msg

    except Exception as e:
        logger.error(f"post_album_message failed in #{channel.name}: {e}")
        return None

    
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name != BotConstants.MUSIC_TOWN_CHANNEL:
        return

    url_match = re.search(r"(https?://\S+)", message.content)
    if not url_match:
        return

    url = url_match.group(1)
    logger.info(f"[AUTO-SAVE] Detected link: {url}")

    # 🚫 Skip non-music domains
    if is_ignored_domain(url):
        logger.info("[AUTO-SAVE] Skipping non-music-related link.")
        return

    fake_interaction = FakeInteraction(message=message, user=message.author)

    try:
        logger.info("[AUTO-SAVE] Calling handle_save_logic...")
        await handle_save_logic(fake_interaction, url, mirror_only=True)
        logger.info("[AUTO-SAVE] handle_save_logic completed.")
        await message.delete()  # Optional
    except Exception as e:
        logger.error(f"[AUTO-SAVE ERROR]: {type(e).__name__}: {e}")


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
            music_town = discord.utils.get(guild.text_channels, name=BotConstants.MUSIC_TOWN_CHANNEL)
            if music_town:
                reactions = []
                if download_available:
                    reactions.append("📥")
                reactions.extend(["📌", "🔁", "❌"])
                await post_album_message(music_town, embed, url, user.id, artist, album, links=links, view=view, extra_reactions=reactions)

        else:
            # === /save or manual 📌 → post to user's private listen-later channel ===
            channel_name = f"{BotConstants.LISTEN_LATER_PREFIX}{user.name.lower().replace(' ', '-')}"

            private = discord.utils.get(guild.text_channels, name=channel_name)
            if not private:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    user: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                }
                private = await guild.create_text_channel(channel_name, overwrites=overwrites)
                logger.info(f"Created new channel: {channel_name}")

            reactions = []
            if download_available:
                reactions.append("📥")
            reactions.extend(["🎧", "🔁", "❌"])
            await post_album_message(private, embed, url, user.id, artist, album, links=links, view=view, extra_reactions=reactions)

    except Exception as e:
        logger.error(f"handle_save_logic failed: {e}")
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
            logger.warning(f"hotupdates links failed: {e}")
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

    channel = discord.utils.get(interaction.guild.text_channels, name=BotConstants.HOT_UPDATES_CHANNEL)
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


@bot.tree.command(name="clear-commands", description="[Admin] Clear all slash commands from this server")
@commands.is_owner()
async def clear_commands(interaction: discord.Interaction):
    """Clear all slash commands from the current guild."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    try:
        # Clear all commands from the guild
        bot.tree.clear_commands(guild=config.DEV_GUILD)
        await bot.tree.sync(guild=config.DEV_GUILD)
        
        await interaction.followup.send("🧹 All slash commands cleared from this server.", ephemeral=True)
        
    except Exception as e:
        logger.error(f"Failed to clear commands: {e}")
        await interaction.followup.send(f"❌ Failed to clear commands: {e}", ephemeral=True)


@bot.tree.command(name="resync", description="[Admin] Clear and re-sync all commands")
@commands.is_owner()
async def resync_commands(interaction: discord.Interaction):
    """Clear all commands and re-sync them."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    try:
        # Clear all commands first
        bot.tree.clear_commands(guild=config.DEV_GUILD)
        await bot.tree.sync(guild=config.DEV_GUILD)
        
        # Wait a moment
        await asyncio.sleep(1)
        
        # Re-sync all current commands
        bot.tree.copy_global_to(guild=config.DEV_GUILD)
        await bot.tree.sync(guild=config.DEV_GUILD)
        
        await interaction.followup.send("🔄 Commands cleared and re-synced successfully.", ephemeral=True)
        
    except Exception as e:
        logger.error(f"Failed to resync commands: {e}")
        await interaction.followup.send(f"❌ Failed to resync commands: {e}", ephemeral=True)


@tasks.loop(time=datetime.time(hour=BotConstants.HOT_UPDATES_HOUR, minute=BotConstants.HOT_UPDATES_MINUTE))
async def scheduled_hotupdates():
    now = datetime.datetime.now()
    if now.weekday() in BotConstants.HOT_UPDATES_DAYS:  # Wednesday and Friday
        guild = bot.get_guild(config.DEV_GUILD_ID)
        if guild:
            channel = discord.utils.get(guild.text_channels, name=BotConstants.HOT_UPDATES_CHANNEL)
            if channel:
                await post_hotupdates(channel)


@scheduled_hotupdates.before_loop
async def before_scheduled_hotupdates():
    await bot.wait_until_ready()


# ==================== LISTENING ROOM COMMANDS ====================

async def album_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete for album names from Plex library."""
    if not config.PLEX_TOKEN or not config.PLEX_URL or len(current) < 2:
        return []
    
    try:
        headers = {"X-Plex-Token": config.PLEX_TOKEN}
        search_url = f"{config.PLEX_URL}/library/search?query={sanitize_query(current)}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, headers=headers, timeout=aiohttp.ClientTimeout(total=BotConstants.PLEX_SEARCH_TIMEOUT)) as response:
                if response.status != 200:
                    return []
                xml = await response.text()
        
        root = ET.fromstring(xml)
        albums = []
        
        for elem in root.findall(".//Directory") + root.findall(".//Video"):
            if elem.attrib.get("type") != "album":
                continue
            
            title = elem.attrib.get("title", "")
            artist = elem.attrib.get("parentTitle", "")
            combined = f"{artist} - {title}"
            
            if len(combined) <= 100:  # Discord choice limit
                albums.append(app_commands.Choice(name=combined, value=combined))
        
        # Sort by relevance and return top results
        albums.sort(key=lambda x: fuzz.ratio(current.lower(), x.name.lower()), reverse=True)
        return albums[:BotConstants.MAX_AUTOCOMPLETE_RESULTS]
        
    except Exception as e:
        logger.warning(f"Autocomplete error: {e}")
        return []


@bot.tree.command(name="golive", description="Start a listening room for an album")
@app_commands.describe(
    source="Album name to search in your library OR Apple Music URL",
    album_name="Specific album name if searching library",
    silent="Create room silently without public announcement (default: False)"
)
@app_commands.autocomplete(source=album_autocomplete)
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
                
                # Use fuzzy matching to find the best match
                from fuzzywuzzy import fuzz
                
                # Score each album against the search query
                for album_data in albums:
                    combined = f"{album_data['artist']} {album_data['title']}"
                    album_data['score'] = fuzz.token_sort_ratio(search_query.lower(), combined.lower())
                
                # Sort by score and take the best match
                albums.sort(key=lambda x: x['score'], reverse=True)
                best_album = albums[0]
                
                logger.info(f"Search '{search_query}' found {len(albums)} albums, best match: {best_album['artist']} - {best_album['title']} (score: {best_album['score']})")
                
                # If the best match is still pretty bad, show some options
                if best_album['score'] < BotConstants.FUZZY_MATCH_THRESHOLD and len(albums) > 1:
                    # Show top 3 matches for user to choose from
                    options_text = "🔍 **Found multiple matches:**\n"
                    for i, album_data in enumerate(albums[:3]):
                        options_text += f"{i+1}. **{album_data['artist']}** - *{album_data['title']}* (match: {album_data['score']}%)\n"
                    options_text += f"\nUsing best match: **{best_album['artist']} - {best_album['title']}**"
                    await interaction.followup.send(options_text)
                
                artist = best_album["artist"]
                album = best_album["title"]
                source_type = "local"
                source_data = best_album["key"]
                
            except Exception as e:
                logger.error(f"Failed to search Plex library: {e}")
                await interaction.followup.send("❌ Failed to search your music library.")
                return
        
        # Create the listening room
        room = await room_manager.create_room(
            host=interaction.user,
            guild=interaction.guild,
            artist=artist,
            album=album,
            source_type=source_type,
            source_data=source_data
        )
        
        # Create a category for this listening room with notification settings
        category_name = f"🎵 {album} - {interaction.user.display_name}"
        try:
            # Create category with notification settings
            # Category permissions - users can control their own voice state
            category_overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(
                    view_channel=False,  # Hide category until setup completes
                    connect=True,  # Can join voice channels
                    speak=False,  # Users start muted but can unmute themselves
                    use_voice_activation=True,  # Enable voice activation
                    send_messages=True,  # Can send messages
                    add_reactions=True,  # Can react to messages
                    # Mute notifications for everyone by default
                    mention_everyone=False,  # Cannot mention @everyone
                ),
                interaction.user: discord.PermissionOverwrite(
                    # Room creator gets full permissions including notifications
                    view_channel=True,
                    connect=True,
                    speak=True,  # Room creator can always speak
                    use_voice_activation=True,
                    send_messages=True,
                    add_reactions=True,
                    mention_everyone=True,  # Can mention @everyone
                ),
                interaction.guild.me: discord.PermissionOverwrite(
                    # Bot needs full permissions
                    view_channel=True,
                    connect=True,
                    speak=True,
                    use_voice_activation=True,
                    manage_channels=True,
                    send_messages=True,
                    mention_everyone=True,
                )
            }
            
            room.category = await interaction.guild.create_category(
                name=category_name,
                overwrites=category_overwrites,
                reason="Listening room category created"
            )
            
        except Exception as e:
            logger.error(f"Failed to create category: {e}")
            await room_manager.cleanup_room(room.room_id)
            await interaction.followup.send("❌ Failed to create listening room category.")
            return
        
        # Create voice channel
        voice_channel_name = f"🎵 {album} - {interaction.user.display_name}"
        
        try:
            # Set up permissions for listening room - users can unmute themselves
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(
                    speak=False,  # Users start muted but can unmute themselves
                    use_voice_activation=True,  # Enable voice activation
                    view_channel=False,  # Private by default; revealed per-user on join
                    connect=False,  # Private by default
                    send_messages=False,  # Private by default
                    add_reactions=False  # Private by default
                ),
                interaction.user: discord.PermissionOverwrite(
                    # Room creator can speak and use voice activation
                    speak=True,  # Room creator can always speak
                    use_voice_activation=True,
                    view_channel=True,
                    connect=True,
                    send_messages=True,
                    add_reactions=True
                ),
                interaction.guild.me: discord.PermissionOverwrite(
                    speak=True,  # Bot can speak (for music)
                    connect=True,
                    use_voice_activation=True,
                    manage_channels=True,
                    send_messages=True
                )
            }
            
            # Determine an appropriate bitrate (Discord caps depend on server boost)
            bitrate_bps = BotConstants.AUDIO_BITRATE_BPS
            room.voice_channel = await interaction.guild.create_voice_channel(
                name=voice_channel_name,
                category=room.category,  # Use the created category
                user_limit=room.max_participants,
                bitrate=bitrate_bps,
                overwrites=overwrites,
                reason="Listening room created"  # Add reason for audit log
            )
            
            # Create persistent text channel
            text_channel_name = f"💬-{album.lower().replace(' ', '-')}-chat"
            room.text_channel = await interaction.guild.create_text_channel(
                name=text_channel_name,
                category=room.category,  # Use the created category
                topic=f"Chat for listening to {artist} - {album} | Room ID: {room.room_id}",
                overwrites=overwrites,  # Use same permission overwrites
                reason="Listening room created"  # Add reason for audit log
            )

            # Keep private by default; access is granted to members when they join
            
        except Exception as e:
            logger.error(f"Failed to create channels: {e}")
            # Clean up category if channel creation fails
            if room.category:
                try:
                    await room.category.delete(reason="Channel creation failed")
                except:
                    pass
            await room_manager.cleanup_room(room.room_id)
            await interaction.followup.send("❌ Failed to create voice/text channels.")
            return
        
        # Create room announcement embed
        embed = discord.Embed(
            title=f"🎵 Listening Room Created",
            description=f"**{artist} - {album}**\n\nRoom ID: `{room.room_id}`\n\nAnyone can join and control the music!\n🔇 *Mics muted by default for focused listening*\n🔕 *Notifications muted for everyone except the room creator*",
            color=discord.Color.purple()
        )
        embed.add_field(
            name="📁 Category", 
            value=room.category.mention, 
            inline=True
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
                f"Category: {room.category.mention}\n"
                f"Voice: {room.voice_channel.mention}\n"
                f"Chat: {room.text_channel.mention}\n\n"
                f"Share the room ID or channels privately!",
                ephemeral=True
            )
        else:
            # Public mode: everyone can see, but no pings/notifications
            await interaction.followup.send(embed=embed, view=view)
        
        # Send initial message to the text channel (with notification suppression)
        welcome_msg = f"🎵 **Welcome to the listening room for {artist} - {album}!**\n\n"
        welcome_msg += f"• Join the voice channel: {room.voice_channel.mention}\n"
        welcome_msg += f"• Anyone can control the music using the buttons below\n"
        welcome_msg += f"• Room ID: `{room.room_id}`"
        
        # Suppress mentions to avoid pings/notifications
        await room.text_channel.send(welcome_msg, allowed_mentions=discord.AllowedMentions.none())
        
        # Load tracks and start audio setup
        loading_msg = await room.text_channel.send("📀 Loading tracks...", allowed_mentions=discord.AllowedMentions.none())
        
        # Load album tracks
        tracks_loaded = await room.load_tracks()
        if not tracks_loaded:
            await loading_msg.edit(content="❌ Failed to load tracks for this album.")
            await room_manager.cleanup_room(room.room_id)
            return
        
        # Connect to voice channel
        voice_connected = await room.connect_voice()
        if not voice_connected:
            await loading_msg.edit(content="❌ Failed to connect to voice channel.")
            await room_manager.cleanup_room(room.room_id)
            return
        
        # Create now playing embed and controls
        now_playing_embed = await create_now_playing_embed(room)
        playback_controls = PlaybackControlView(room.room_id)
        await playback_controls.update_buttons(room)
        
        # Send the now playing interface
        await loading_msg.edit(
            content=f"✅ Loaded {len(room.tracks)} tracks! Ready to play.",
            embed=now_playing_embed,
            view=playback_controls
        )
        
        # Store the message reference for UI updates
        room.now_playing_message = loading_msg
        
        # Auto-start playback with retries
        await asyncio.sleep(2)  # Give more time for voice connection to stabilize
        
        auto_start_success = False
        for attempt in range(3):  # Try 3 times
            logger.info(f"Auto-start attempt {attempt + 1}/3 for room {room.room_id}")
            
            # Check voice connection before trying to play
            if not room.voice_client or not room.voice_client.is_connected():
                logger.warning(f"Voice not connected on attempt {attempt + 1}, reconnecting...")
                await room.connect_voice()
                await asyncio.sleep(1)
            
            success = await room.play_current_track()
            if success:
                auto_start_success = True
                logger.info(f"Auto-start successful on attempt {attempt + 1}")
                break
            else:
                logger.warning(f"Auto-start failed on attempt {attempt + 1}")
                await asyncio.sleep(2)  # Wait before retry
        
        # Update UI based on success/failure
        if auto_start_success:
            # Update the embed to show now playing
            updated_embed = await create_now_playing_embed(room)
            await playback_controls.update_buttons(room)
            await loading_msg.edit(
                content=f"🎵 **Now playing {artist} - {album}**",
                embed=updated_embed,
                view=playback_controls
            )
            await room.text_channel.send(f"🎵 Auto-started playback! **{room.current_track_info}**", allowed_mentions=discord.AllowedMentions.none())
        else:
            logger.error(f"Failed to auto-start playback after 3 attempts in room {room.room_id}")
            await room.text_channel.send("⚠️ Tracks loaded but auto-start failed. Use the ▶️ button to start manually.", allowed_mentions=discord.AllowedMentions.none())
            await room.text_channel.send("💡 **Tip:** Try `/debug` and `/reconnect` if you have issues.", allowed_mentions=discord.AllowedMentions.none())
        
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
        # If they're marked as in a room but not actually in its participant list, clean it
        if not any(p.id == interaction.user.id for p in current_room.participants):
            await room_manager.leave_room(interaction.user)
        else:
            await interaction.followup.send(f"❌ You're already in room `{current_room.room_id}`. Leave it first with `/leave`.")
            return
    
    # Try to join the room
    success = await room_manager.join_room(room_id, interaction.user)
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

    # Grant this member access to private channels
    try:
        await room.category.set_permissions(interaction.user, view_channel=True)
        await room.text_channel.set_permissions(interaction.user, view_channel=True, send_messages=True, add_reactions=True)
        await room.voice_channel.set_permissions(interaction.user, view_channel=True, connect=True, speak=True, use_voice_activation=True)
    except Exception:
        pass

    # Announce in the text channel
    if room.text_channel:
        await room.text_channel.send(f"👋 **{interaction.user.display_name}** joined the room! ({len(room.participants)}/{room.max_participants})", allowed_mentions=discord.AllowedMentions.none())


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
    try:
        await interaction.followup.send(f"🧹 Cleaned up {rooms_count} listening rooms and their channels.")
    except discord.errors.NotFound:
        # Original response no longer exists; send a fresh ephemeral confirmation
        await interaction.response.send_message(
            f"🧹 Cleaned up {rooms_count} listening rooms and their channels.",
            ephemeral=True
        )


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
            # If user is not actually in participants (stale mapping), clear it
            if not any(p.id == interaction.user.id for p in current_room.participants):
                await room_manager.leave_room(interaction.user)
            else:
                await interaction.response.send_message(
                    f"❌ You're already in room `{current_room.room_id}`. Leave it first with `/leave`.",
                    ephemeral=True
                )
                return
        
        # Try to join the room
        success = await room_manager.join_room(room_id, interaction.user)
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

    # Grant this member access to private channels (if not already set)
    try:
        await room.category.set_permissions(interaction.user, view_channel=True)
        await room.text_channel.set_permissions(interaction.user, view_channel=True, send_messages=True, add_reactions=True)
        await room.voice_channel.set_permissions(interaction.user, view_channel=True, connect=True, speak=True, use_voice_activation=True)
    except Exception:
        pass

    # Announce in the text channel
    if room.text_channel:
        await room.text_channel.send(f"👋 **{interaction.user.display_name}** joined the room! ({len(room.participants)}/{room.max_participants})", allowed_mentions=discord.AllowedMentions.none())


# ==================== PLAYBACK CONTROL COMMANDS ====================

@bot.tree.command(name="play", description="Start or resume playback in your listening room")
async def play_command(interaction: discord.Interaction):
    """Start or resume playback."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    room = room_manager.get_user_room(interaction.user.id)
    if not room:
        await interaction.followup.send("❌ You're not in any listening room.")
        return
    
    try:
        if room.is_paused:
            await room.resume()
            await interaction.followup.send("▶️ Resumed playback.")
        else:
            success = await room.play_current_track()
            if success:
                track = room.current_track_info
                await interaction.followup.send(f"▶️ Started playing: **{track}**")
            else:
                await interaction.followup.send("❌ Failed to start playback.")
    except Exception as e:
        logger.error(f"Play command error: {e}")
        await interaction.followup.send("❌ Failed to control playback.")


@bot.tree.command(name="pause", description="Pause playback in your listening room")
async def pause_command(interaction: discord.Interaction):
    """Pause playback."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    room = room_manager.get_user_room(interaction.user.id)
    if not room:
        await interaction.followup.send("❌ You're not in any listening room.")
        return
    
    try:
        await room.pause()
        await interaction.followup.send("⏸️ Paused playback.")
    except Exception as e:
        logger.error(f"Pause command error: {e}")
        await interaction.followup.send("❌ Failed to pause playback.")


@bot.tree.command(name="skip", description="Skip to the next track")
async def skip_command(interaction: discord.Interaction):
    """Skip to next track."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    room = room_manager.get_user_room(interaction.user.id)
    if not room:
        await interaction.followup.send("❌ You're not in any listening room.")
        return
    
    try:
        success = await room.skip_to_next()
        if success:
            track = room.current_track_info
            await interaction.followup.send(f"⏭️ Skipped to: **{track}**")
        else:
            await interaction.followup.send("❌ Cannot skip - already at last track.")
    except Exception as e:
        logger.error(f"Skip command error: {e}")
        await interaction.followup.send("❌ Failed to skip track.")


@bot.tree.command(name="back", description="Go back to the previous track")
async def back_command(interaction: discord.Interaction):
    """Go to previous track."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    room = room_manager.get_user_room(interaction.user.id)
    if not room:
        await interaction.followup.send("❌ You're not in any listening room.")
        return
    
    try:
        success = await room.skip_to_previous()
        if success:
            track = room.current_track_info
            await interaction.followup.send(f"⏮️ Went back to: **{track}**")
        else:
            await interaction.followup.send("❌ Cannot go back - already at first track.")
    except Exception as e:
        logger.error(f"Back command error: {e}")
        await interaction.followup.send("❌ Failed to go back to previous track.")


@bot.tree.command(name="nowplaying", description="Show what's currently playing in your room")
async def nowplaying_command(interaction: discord.Interaction):
    """Show current track info."""
    await interaction.response.defer(thinking=True)
    
    room = room_manager.get_user_room(interaction.user.id)
    if not room:
        await interaction.followup.send("❌ You're not in any listening room.")
        return
    
    embed = await create_now_playing_embed(room)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="debug", description="[Debug] Show voice connection status")
@commands.is_owner()
async def debug_voice(interaction: discord.Interaction):
    """Debug voice connection issues."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    room = room_manager.get_user_room(interaction.user.id)
    if not room:
        await interaction.followup.send("❌ You're not in any listening room.")
        return
    
    debug_info = []
    debug_info.append(f"**Room ID:** {room.room_id}")
    debug_info.append(f"**Voice Channel:** {room.voice_channel.name if room.voice_channel else 'None'}")
    debug_info.append(f"**Voice Client:** {'✅ Connected' if room.voice_client and room.voice_client.is_connected() else '❌ Not connected'}")
    
    if room.voice_client:
        debug_info.append(f"**Is Playing:** {'✅' if room.voice_client.is_playing() else '❌'}")
        debug_info.append(f"**Is Paused:** {'✅' if room.voice_client.is_paused() else '❌'}")
        debug_info.append(f"**Latency:** {room.voice_client.latency * 1000:.1f}ms")
        
        # Additional connection diagnostics
        if hasattr(room.voice_client, 'ws') and room.voice_client.ws:
            debug_info.append(f"**WebSocket Open:** {'✅' if not room.voice_client.ws.closed else '❌'}")
            if hasattr(room.voice_client.ws, 'latency'):
                debug_info.append(f"**WS Latency:** {room.voice_client.ws.latency * 1000:.1f}ms")
        
        # Voice channel info
        if room.voice_channel:
            debug_info.append(f"**Channel Members:** {len(room.voice_channel.members)}")
            debug_info.append(f"**Channel ID:** {room.voice_channel.id}")
    
    debug_info.append(f"**Room State - Playing:** {room.is_playing}")
    debug_info.append(f"**Room State - Paused:** {room.is_paused}")
    debug_info.append(f"**Current Track:** {room.current_track_info or 'None'}")
    debug_info.append(f"**Total Tracks:** {len(room.tracks)}")
    
    # Connection history (if available)
    if hasattr(room, '_connection_attempts'):
        debug_info.append(f"**Connection Attempts:** {getattr(room, '_connection_attempts', 0)}")
    
    await interaction.followup.send("\n".join(debug_info))


@bot.tree.command(name="reconnect", description="[Debug] Force reconnect to voice channel")
async def reconnect_voice(interaction: discord.Interaction):
    """Force reconnect to voice channel."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    room = room_manager.get_user_room(interaction.user.id)
    if not room:
        await interaction.followup.send("❌ You're not in any listening room.")
        return
    
    try:
        # Disconnect first
        if room.voice_client:
            await room.voice_client.disconnect()
            room.voice_client = None
        
        # Reconnect
        success = await room.connect_voice()
        if success:
            await interaction.followup.send("✅ Reconnected to voice channel.")
        else:
            await interaction.followup.send("❌ Failed to reconnect to voice channel.")
            
    except Exception as e:
        logger.error(f"Reconnect command error: {e}")
        await interaction.followup.send(f"❌ Error during reconnect: {e}")


@bot.tree.command(name="test_plex", description="[Debug] Test Plex streaming URL")
@app_commands.describe(album_name="Album name to test streaming for")
async def test_plex_streaming(interaction: discord.Interaction, album_name: str):
    """Test Plex streaming URL generation and format detection."""
    if not config.PLEX_TOKEN or not config.PLEX_URL:
        await interaction.response.send_message("❌ Plex integration is not configured.", ephemeral=True)
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    try:
        # Search for the album
        headers = {"X-Plex-Token": config.PLEX_TOKEN}
        search_url = f"{config.PLEX_URL}/library/search?query={sanitize_query(album_name)}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, headers=headers) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Failed to search Plex library.", ephemeral=True)
                    return
                xml = await response.text()
        
        root = ET.fromstring(xml)
        albums = []
        
        for elem in root.findall(".//Directory") + root.findall(".//Video"):
            if elem.attrib.get("type") != "album":
                continue
            
            title = elem.attrib.get("title", "")
            artist = elem.attrib.get("parentTitle", "")
            combined = f"{artist} - {title}"
            score = fuzz.token_sort_ratio(album_name.lower(), combined.lower())
            
            if score > 50:  # Only show relevant matches
                albums.append({
                    "title": title,
                    "artist": artist,
                    "score": score,
                    "key": elem.attrib.get("key", ""),
                    "combined": combined
                })
        
        if not albums:
            await interaction.followup.send(f"❌ No albums found for '{album_name}'.", ephemeral=True)
            return
        
        # Sort by relevance and test the best match
        albums.sort(key=lambda x: x["score"], reverse=True)
        best_match = albums[0]
        
        # Test the streaming URL generation
        album_url = f"{config.PLEX_URL}{best_match['key']}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(album_url, headers=headers) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Failed to fetch album details.", ephemeral=True)
                    return
                album_xml = await response.text()
        
        album_root = ET.fromstring(album_xml)
        tracks_info = []
        
        for track in album_root.findall(".//Track")[:3]:  # Test first 3 tracks
            title = track.attrib.get("title", "Unknown")
            duration = int(track.attrib.get("duration", "0")) // 1000
            
            media_elements = track.findall(".//Media")
            if media_elements:
                best_media = media_elements[0]
                bitrate = int(best_media.attrib.get("bitrate", "0"))
                container = best_media.attrib.get("container", "unknown")
                codec = best_media.attrib.get("codec", "unknown")
                
                part = best_media.find(".//Part")
                if part is not None:
                    file_key = part.attrib.get("key", "")
                    streaming_url = f"{config.PLEX_URL}{file_key}?X-Plex-Token={config.PLEX_TOKEN}"
                    
                    tracks_info.append({
                        "title": title,
                        "duration": duration,
                        "bitrate": bitrate,
                        "container": container,
                        "codec": codec,
                        "url": streaming_url[:80] + "..." if len(streaming_url) > 80 else streaming_url
                    })
        
        # Build response
        embed = discord.Embed(
            title="🔍 Plex Streaming Test",
            description=f"**Best Match:** {best_match['combined']} (Score: {best_match['score']}%)",
            color=discord.Color.blue()
        )
        
        if tracks_info:
            track_details = ""
            for i, track in enumerate(tracks_info, 1):
                track_details += f"**{i}. {track['title']}**\n"
                track_details += f"   Duration: {track['duration']}s | Bitrate: {track['bitrate']}kbps\n"
                track_details += f"   Format: {track['container']}/{track['codec']}\n"
                track_details += f"   URL: `{track['url']}`\n\n"
            
            embed.add_field(name="Track Details", value=track_details, inline=False)
        else:
            embed.add_field(name="Tracks", value="❌ No tracks found", inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Plex test error: {e}")
        await interaction.followup.send(f"❌ Test failed: {e}", ephemeral=True)


@bot.tree.command(name="test_audio", description="[Debug] Test audio streaming with current FFmpeg settings")
@app_commands.describe(album_name="Album name to test audio streaming for")
async def test_audio_streaming(interaction: discord.Interaction, album_name: str):
    """Test audio streaming with current FFmpeg configuration."""
    if not config.PLEX_TOKEN or not config.PLEX_URL:
        await interaction.response.send_message("❌ Plex integration is not configured.", ephemeral=True)
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    try:
        # Search for the album
        headers = {"X-Plex-Token": config.PLEX_TOKEN}
        search_url = f"{config.PLEX_URL}/library/search?query={sanitize_query(album_name)}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, headers=headers) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Failed to search Plex library.", ephemeral=True)
                    return
                xml = await response.text()
        
        root = ET.fromstring(xml)
        best_match = None
        best_score = 0
        
        for elem in root.findall(".//Directory") + root.findall(".//Video"):
            if elem.attrib.get("type") != "album":
                continue
            
            title = elem.attrib.get("title", "")
            artist = elem.attrib.get("parentTitle", "")
            combined = f"{artist} - {title}"
            score = fuzz.token_sort_ratio(album_name.lower(), combined.lower())
            
            if score > best_score:
                best_score = score
                best_match = {
                    "title": title,
                    "artist": artist,
                    "key": elem.attrib.get("key", ""),
                    "combined": combined
                }
        
        if not best_match or best_score < 50:
            await interaction.followup.send(f"❌ No suitable album found for '{album_name}'.", ephemeral=True)
            return
        
        # Test simplified FFmpeg configuration (based on research)
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'
        }
        
        # Get first track URL
        album_url = f"{config.PLEX_URL}{best_match['key']}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(album_url, headers=headers) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Failed to fetch album details.", ephemeral=True)
                    return
                album_xml = await response.text()
        
        album_root = ET.fromstring(album_xml)
        first_track = album_root.find(".//Track")
        
        if first_track is None:
            await interaction.followup.send("❌ No tracks found in album.", ephemeral=True)
            return
        
        # Get track details
        title = first_track.attrib.get("title", "Unknown")
        duration = int(first_track.attrib.get("duration", "0")) // 1000
        
        media_elements = first_track.findall(".//Media")
        if not media_elements:
            await interaction.followup.send("❌ No media found for track.", ephemeral=True)
            return
        
        best_media = media_elements[0]
        bitrate = int(best_media.attrib.get("bitrate", "0"))
        container = best_media.attrib.get("container", "unknown")
        codec = best_media.attrib.get("codec", "unknown")
        
        part = best_media.find(".//Part")
        if part is None:
            await interaction.followup.send("❌ No audio part found for track.", ephemeral=True)
            return
        
        file_key = part.attrib.get("key", "")
        streaming_url = f"{config.PLEX_URL}{file_key}?X-Plex-Token={config.PLEX_TOKEN}"
        
        # Test FFmpeg source creation with multiple fallbacks
        ffmpeg_status = "❌ All methods failed"
        try:
            # Try FFmpegOpusAudio first (Discord-native format)
            source = discord.FFmpegOpusAudio(streaming_url, **ffmpeg_options)
            ffmpeg_status = "✅ FFmpegOpusAudio Success"
        except Exception as e:
            try:
                # Fallback to FFmpegPCMAudio
                source = discord.FFmpegPCMAudio(streaming_url, **ffmpeg_options)
                ffmpeg_status = "✅ FFmpegPCMAudio Success"
            except Exception as e2:
                try:
                    # Last resort with explicit Opus encoding
                    alternative_options = {
                        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                        'options': '-vn -acodec libopus -ar 48000 -ac 2'
                    }
                    source = discord.FFmpegOpusAudio(streaming_url, **alternative_options)
                    ffmpeg_status = "✅ FFmpegOpusAudio (Alternative) Success"
                except Exception as e3:
                    ffmpeg_status = f"❌ All methods failed: {str(e3)[:100]}"
        
        # Build response
        embed = discord.Embed(
            title="🎵 Audio Streaming Test",
            description=f"**Album:** {best_match['combined']} (Score: {best_score}%)",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="Track Info", value=f"**Title:** {title}\n**Duration:** {duration}s\n**Bitrate:** {bitrate}kbps\n**Format:** {container}/{codec}", inline=False)
        embed.add_field(name="FFmpeg Test", value=ffmpeg_status, inline=False)
        embed.add_field(name="Streaming URL", value=f"`{streaming_url[:80]}...`", inline=False)
        embed.add_field(name="FFmpeg Options", value=f"```{ffmpeg_options['options']}```", inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Test audio streaming error: {e}")
        await interaction.followup.send(f"❌ Error testing audio streaming: {e}", ephemeral=True)


@bot.tree.command(name="quality", description="Show current audio quality settings")
async def show_quality(interaction: discord.Interaction):
    """Show current audio quality configuration."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    
    quality_info = []
    quality_info.append("🎧 **Audio Quality Settings**")
    quality_info.append(f"**Source Quality:** {'✅ High' if config.HIGH_QUALITY_AUDIO else '⚠️ Standard'}")
    quality_info.append(f"**Discord Output:** ~96kbps Opus (Discord's limit)")
    quality_info.append("")
    quality_info.append("**Source Formats:**")
    if config.HIGH_QUALITY_AUDIO:
        quality_info.append("• **Apple Music:** ALAC/FLAC → Opus (lossless source)")
        quality_info.append("• **Plex:** Highest bitrate → Opus (best available)")
    else:
        quality_info.append(f"• **Apple Music:** {config.GAMDL_CODEC} → Opus")
        quality_info.append("• **Plex:** Native format → Opus")
    quality_info.append("")
    quality_info.append("**Discord Processing:**")
    quality_info.append("• Opus 96kbps @ 48kHz stereo (Discord's maximum)")
    quality_info.append("• Maximum compression quality (level 10)")
    quality_info.append("• Constrained VBR for consistent quality")
    quality_info.append("• Frequency optimization (10Hz-20kHz)")
    quality_info.append("• Optimized for music (not voice)")
    
    quality_info.append("\n**Quality Comparison:**")
    quality_info.append("• 96kbps Opus ≈ 128-160kbps MP3 quality")
    quality_info.append("• Opus is more efficient than MP3")
    quality_info.append("• Optimized for real-time music streaming")
    
    quality_info.append("\n**Plex Streaming Improvements:**")
    quality_info.append("• ✅ Enhanced multi-strategy voice connection")
    quality_info.append("• ✅ Progressive backoff retry logic (5s, 10s, 15s)")
    quality_info.append("• ✅ Improved FFmpeg process management")
    quality_info.append("• ✅ Real-time UI updates for track changes")
    
    quality_info.append("\n**Recent Connection Improvements:**")
    quality_info.append("• ✅ Multiple connection strategies (minimal, timeout, auto-reconnect)")
    quality_info.append("• ✅ Enhanced FFmpeg process termination handling")
    quality_info.append("• ✅ Fixed player button functionality")
    quality_info.append("• ✅ Automatic track info updates in UI")
    
    quality_info.append("\n⚠️ **Note:** Discord has a hard 96kbps limit")
    quality_info.append("We've optimized for the best possible quality within this limit!")
    quality_info.append("\n💡 **Current Mode:** Ultra-minimal streaming with no conversion")
    
    await interaction.followup.send("\n".join(quality_info))


@bot.tree.command(name="search", description="Search your music library for albums")
@app_commands.describe(query="Artist or album name to search for")
async def search_albums(interaction: discord.Interaction, query: str):
    """Search for albums in the music library."""
    await interaction.response.defer(thinking=True)
    
    if not config.PLEX_TOKEN or not config.PLEX_URL:
        await interaction.followup.send("❌ Plex integration is not configured.")
        return
    
    try:
        headers = {"X-Plex-Token": config.PLEX_TOKEN}
        sanitized_query = sanitize_query(query)
        search_url = f"{config.PLEX_URL}/library/search?query={sanitized_query}"
        
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
            artist = elem.attrib.get("parentTitle", "")
            year = elem.attrib.get("year", "")
            
            # Calculate relevance score
            combined = f"{artist} {title}"
            score = fuzz.token_sort_ratio(query.lower(), combined.lower())
            
            albums.append({
                "artist": artist,
                "title": title,
                "year": year,
                "score": score,
                "combined": f"{artist} - {title}" + (f" ({year})" if year else "")
            })
        
        if not albums:
            await interaction.followup.send(f"❌ No albums found for '{query}' in your library.")
            return
        
        # Sort by relevance and show top results
        albums.sort(key=lambda x: x["score"], reverse=True)
        top_albums = albums[:BotConstants.MAX_SEARCH_RESULTS]
        
        embed = discord.Embed(
            title="🔍 Album Search Results",
            description=f"Found {len(albums)} albums for: **{query}**",
            color=discord.Color.blue()
        )
        
        results_text = ""
        for i, album in enumerate(top_albums, 1):
            match_indicator = "🎯" if album["score"] > 80 else "✨" if album["score"] > 60 else "📀"
            results_text += f"{match_indicator} **{album['combined']}**\n"
        
        embed.add_field(name="Top Matches", value=results_text, inline=False)
        embed.set_footer(text="💡 Use /golive with the exact album name to start a listening room")
        
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Search command error: {e}")
        await interaction.followup.send("❌ Error occurred while searching your music library.")


# ==================== END PLAYBACK CONTROL COMMANDS ====================

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
        logger.error(f"mark_as_listened failed: {e}")


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
        logger.error(f"recommend_album failed: {e}")


# Scrobbling Commands
@bot.tree.command(name="scrobble_setup", description="Set up Last.fm scrobbling for your account")
@app_commands.describe(username="Your Last.fm username")
async def setup_scrobbling(interaction: discord.Interaction, username: str):
    """Set up Last.fm scrobbling for a user."""
    await interaction.response.defer(ephemeral=True)
    
    if not PYLAST_AVAILABLE:
        await interaction.followup.send(
            "❌ **Last.fm scrobbling is not available.**\n"
            "The `pylast` library is not installed."
        )
        return
    
    if not config.LASTFM_API_KEY or not config.LASTFM_API_SECRET:
        await interaction.followup.send(
            "❌ **Last.fm scrobbling is not configured on this bot.**\n"
            "The bot admin needs to set `LASTFM_API_KEY` and `LASTFM_API_SECRET` environment variables."
        )
        return
    
    # Generate auth URL (now async)
    auth_url = await scrobble_manager.get_auth_url()
    if not auth_url:
        await interaction.followup.send("❌ Failed to generate Last.fm authentication URL.")
        return
    
    # Create auth view with buttons
    view = ScrobbleAuthView(interaction.user.id, username, auth_url)
    
    embed = discord.Embed(
        title="🎵 Last.fm Scrobbling Setup",
        description=(
            f"**Step 1:** Click the button below to authorize this bot with your Last.fm account\n"
            f"**Step 2:** After authorizing, click 'Complete Setup' to finish\n\n"
            f"**Username:** {username}\n"
            f"**Note:** You'll be able to scrobble tracks in listening rooms!"
        ),
        color=0xd51007  # Last.fm red
    )
    embed.set_footer(text="Your Last.fm credentials are stored securely and only used for scrobbling.")
    
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="scrobble_status", description="Check your Last.fm scrobbling status")
async def scrobble_status(interaction: discord.Interaction):
    """Check user's scrobbling status."""
    await interaction.response.defer(ephemeral=True)
    
    user = scrobble_manager.get_user(interaction.user.id)
    
    if not user:
        embed = discord.Embed(
            title="🎵 Scrobbling Status",
            description=(
                "❌ **Not set up**\n\n"
                "Use `/scrobble_setup` to connect your Last.fm account!\n\n"
                "After setup, tracks will scrobble when either half the track or 30s has played (whichever is less)."
            ),
            color=0xff0000
        )
    else:
        embed = discord.Embed(
            title="🎵 Scrobbling Status",
            description=(
                f"✅ **Connected to Last.fm**\n\n**Username:** {user.lastfm_username}\n"
                "**Status:** Ready to scrobble!\n\n"
                "Tracks scrobble after 30s or half duration (whichever is less)."
            ),
            color=0x00ff00
        )
        embed.set_footer(text="Tracks will be scrobbled automatically when you listen in rooms.")
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="scrobble_remove", description="Remove Last.fm scrobbling from your account")
async def remove_scrobbling(interaction: discord.Interaction):
    """Remove user's scrobbling configuration."""
    await interaction.response.defer(ephemeral=True)
    
    removed = scrobble_manager.remove_user(interaction.user.id)
    
    if removed:
        embed = discord.Embed(
            title="🎵 Scrobbling Removed",
            description="✅ Your Last.fm connection has been removed.\n\nYour listening activity will no longer be scrobbled.",
            color=0xff9900
        )
    else:
        embed = discord.Embed(
            title="🎵 Scrobbling Not Found",
            description="❌ You don't have Last.fm scrobbling set up.\n\nUse `/scrobble_setup` to connect your account!",
            color=0xff0000
        )
    
    await interaction.followup.send(embed=embed)


class ScrobbleAuthView(discord.ui.View):
    """View for Last.fm authentication process."""
    
    def __init__(self, user_id: int, username: str, auth_url: str):
        super().__init__(timeout=300)  # 5 minutes
        self.user_id = user_id
        self.username = username
        
        # Create the link button manually
        link_button = discord.ui.Button(
            label="Authorize Last.fm",
            style=discord.ButtonStyle.link,
            url=auth_url,
            emoji="🔗"
        )
        self.add_item(link_button)
    
    @discord.ui.button(label="Complete Setup", style=discord.ButtonStyle.green, emoji="✅")
    async def complete_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Complete the scrobbling setup."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Only the person who started this setup can complete it.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Get the token from the authorization URL and exchange for session key
        try:
            # For now, we need the user to provide the token manually
            # In a full implementation, we'd capture this from a callback URL
            embed = discord.Embed(
                title="🎵 Almost Done!",
                description=(
                    "**Step 1:** Click the authorize button above if you haven't already\n"
                    "**Step 2:** After authorizing on Last.fm, look for a token in the URL\n"
                    "**Step 3:** Use `/scrobble_token <token>` with that token to complete setup\n\n"
                    f"**Username:** {self.username}\n\n"
                    "*The token will be in the URL after authorization like:*\n"
                    "`...&token=abc123def456...`"
                ),
                color=0xff9900
            )
            embed.set_footer(text="Looking for the token in the URL after you authorize!")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            await interaction.followup.send(f"❌ Setup error: {e}", ephemeral=True)


@bot.tree.command(name="test_new_command", description="Test if new commands are working")
async def test_new_command(interaction: discord.Interaction):
    """Test command to verify new commands are being registered."""
    await interaction.response.send_message("✅ New commands are working! Scrobbling commands should be available too.", ephemeral=True)


@bot.tree.command(name="scrobble_token", description="Complete Last.fm setup with your authorization token")
@app_commands.describe(token="The token from Last.fm after authorization")
async def complete_scrobble_setup(interaction: discord.Interaction, token: str):
    """Complete scrobbling setup using the authorization token."""
    await interaction.response.defer(ephemeral=True)
    
    if not PYLAST_AVAILABLE:
        await interaction.followup.send("❌ Last.fm scrobbling is not available.")
        return
    
    try:
        # Exchange token for session key
        session_key = await scrobble_manager.get_session_key(token)
        if not session_key:
            await interaction.followup.send("❌ Failed to exchange token for session key. Make sure you authorized the application first.")
            return
        
        # We need the username - for now, let's get it from Last.fm
        network = pylast.LastFMNetwork(
            api_key=config.LASTFM_API_KEY,
            api_secret=config.LASTFM_API_SECRET,
            session_key=session_key
        )
        
        # Get the user's Last.fm username
        user_info = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: network.get_authenticated_user()
        )
        username = user_info.name
        
        # Add the user to our scrobbling system
        scrobble_manager.add_user(interaction.user.id, username, session_key)
        
        embed = discord.Embed(
            title="✅ Last.fm Scrobbling Enabled!",
            description=(
                f"**Username:** {username}\n**Status:** Ready to scrobble!\n\n"
                "Your listening activity in rooms will now scrobble automatically.\n"
                "We persist your credentials in a secure file so you won't need to re-link after restarts."
            ),
            color=0x00ff00
        )
        embed.set_footer(text="Scrobbles send after 30s or half track duration, whichever is less.")
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Failed to complete scrobble setup: {e}")
        await interaction.followup.send(f"❌ Failed to complete setup: {e}")


@bot.tree.command(name="scrobble_add_user", description="[Admin] Manually add a user's Last.fm session key")
@app_commands.describe(
    user="Discord user to add scrobbling for",
    lastfm_username="Their Last.fm username", 
    session_key="Their Last.fm session key"
)
async def add_scrobble_user(interaction: discord.Interaction, user: discord.Member, lastfm_username: str, session_key: str):
    """Admin command to manually add a user's scrobbling configuration."""
    # Check if user is admin
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can use this command.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        scrobble_manager.add_user(user.id, lastfm_username, session_key)
        
        embed = discord.Embed(
            title="✅ Scrobbling User Added",
            description=f"**User:** {user.mention}\n**Last.fm:** {lastfm_username}\n\nThey can now scrobble tracks in listening rooms!",
            color=0x00ff00
        )
        await interaction.followup.send(embed=embed)
        
        # Notify the user
        try:
            await user.send(f"🎵 **Scrobbling Enabled!**\n\nYour Last.fm account ({lastfm_username}) has been connected to the music bot. Your listening activity in rooms will now be scrobbled automatically!")
        except:
            pass  # User might have DMs disabled
            
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to add scrobbling user: {e}")


if __name__ == "__main__":
    bot.run(config.TOKEN)