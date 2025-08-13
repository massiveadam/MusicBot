"""
Configuration constants for the Discord Music Bot.
All hardcoded values should be moved here and made configurable.
"""

import os
from typing import List

class BotConstants:
    """Constants for bot behavior and limits."""
    
    # Room Management
    MAX_ROOM_PARTICIPANTS = int(os.getenv("MAX_ROOM_PARTICIPANTS", "5"))
    ROOM_ID_LENGTH = int(os.getenv("ROOM_ID_LENGTH", "8"))
    
    # Voice Connection
    VOICE_CONNECT_TIMEOUT = int(os.getenv("VOICE_CONNECT_TIMEOUT", "45"))
    VOICE_RETRY_ATTEMPTS = int(os.getenv("VOICE_RETRY_ATTEMPTS", "8"))
    VOICE_BASE_DELAY = float(os.getenv("VOICE_BASE_DELAY", "2.0"))
    VOICE_MAX_DELAY = float(os.getenv("VOICE_MAX_DELAY", "32.0"))
    VOICE_STABILIZE_DELAY = float(os.getenv("VOICE_STABILIZE_DELAY", "2.0"))
    
    # Audio Settings
    AUDIO_BITRATE_BPS = int(os.getenv("AUDIO_BITRATE_BPS", "96000"))
    AUDIO_SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "48000"))
    AUDIO_CHANNELS = int(os.getenv("AUDIO_CHANNELS", "2"))
    
    # Scrobbling
    MIN_SCROBBLE_TIME = int(os.getenv("MIN_SCROBBLE_TIME", "30"))
    SCROBBLE_PERCENTAGE = float(os.getenv("SCROBBLE_PERCENTAGE", "0.5"))
    
    # File Management
    TEMP_DIR_PREFIX = os.getenv("TEMP_DIR_PREFIX", "listening_room_")
    MAX_FILENAME_LENGTH = int(os.getenv("MAX_FILENAME_LENGTH", "255"))
    
    # Search and Matching
    FUZZY_MATCH_THRESHOLD = int(os.getenv("FUZZY_MATCH_THRESHOLD", "60"))
    MAX_SEARCH_RESULTS = int(os.getenv("MAX_SEARCH_RESULTS", "10"))
    MAX_AUTOCOMPLETE_RESULTS = int(os.getenv("MAX_AUTOCOMPLETE_RESULTS", "10"))
    
    # Timeouts
    HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))
    DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
    PLEX_SEARCH_TIMEOUT = int(os.getenv("PLEX_SEARCH_TIMEOUT", "5"))
    
    # Retry Settings
    RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
    RETRY_DELAY = float(os.getenv("RETRY_DELAY", "1.0"))
    
    # UI Settings
    EMBED_COLOR_DEFAULT = int(os.getenv("EMBED_COLOR_DEFAULT", "0x7289DA"), 16)  # Discord blue
    EMBED_COLOR_SUCCESS = int(os.getenv("EMBED_COLOR_SUCCESS", "0x00FF00"), 16)  # Green
    EMBED_COLOR_ERROR = int(os.getenv("EMBED_COLOR_ERROR", "0xFF0000"), 16)      # Red
    EMBED_COLOR_WARNING = int(os.getenv("EMBED_COLOR_WARNING", "0xFFA500"), 16)  # Orange
    
    # Channel Names
    MUSIC_TOWN_CHANNEL = os.getenv("MUSIC_TOWN_CHANNEL", "music-town")
    HOT_UPDATES_CHANNEL = os.getenv("HOT_UPDATES_CHANNEL", "hot-updates")
    LISTEN_LATER_PREFIX = os.getenv("LISTEN_LATER_PREFIX", "listen-later-")
    
    # Ignored Domains for Auto-Save
    IGNORED_DOMAINS: List[str] = [
        "tenor.com", "giphy.com", "imgur.com", "youtube.com", "youtu.be",
        "twitter.com", "x.com", "reddit.com", "tiktok.com"
    ]
    
    # File Extensions
    AUDIO_EXTENSIONS = ['.mp3', '.flac', '.m4a', '.ogg', '.wma', '.aac']
    IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
    
    # FFmpeg Options
    FFMPEG_BEFORE_OPTIONS = os.getenv("FFMPEG_BEFORE_OPTIONS", 
        "-reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_delay_max 5 -rw_timeout 5000000")
    
    FFMPEG_OPTIONS = os.getenv("FFMPEG_OPTIONS",
        "-vn -probesize 256k -analyzeduration 1M -use_wallclock_as_timestamps 1 -af aresample=async=1:min_hard_comp=0.100:first_pts=0")
    
    FFMPEG_OPUS_OPTIONS = os.getenv("FFMPEG_OPUS_OPTIONS",
        "-vn -acodec libopus -ar 48000 -ac 2 -b:a 96k -vbr constrained -compression_level 10 -application audio -frame_duration 60 -probesize 256k -analyzeduration 1M -use_wallclock_as_timestamps 1 -af aresample=async=1:min_hard_comp=0.100:first_pts=0")
    
    # Scheduled Tasks
    HOT_UPDATES_HOUR = int(os.getenv("HOT_UPDATES_HOUR", "17"))
    HOT_UPDATES_MINUTE = int(os.getenv("HOT_UPDATES_MINUTE", "30"))
    HOT_UPDATES_DAYS = [2, 4]  # Wednesday and Friday (0=Monday)
    
    # Last.fm Colors
    LASTFM_RED = int(os.getenv("LASTFM_RED", "0xD51007"), 16)
    LASTFM_GREEN = int(os.getenv("LASTFM_GREEN", "0x00FF00"), 16)
    LASTFM_ORANGE = int(os.getenv("LASTFM_ORANGE", "0xFF9900"), 16)
