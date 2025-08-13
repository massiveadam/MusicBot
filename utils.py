"""
Utility functions for the Discord Music Bot.
Common functions used across multiple modules.
"""

import os
import re
import tempfile
import shutil
import logging
from typing import Optional, Tuple, List
from urllib.parse import urlparse, quote_plus
import unicodedata
from pathlib import Path

from config_constants import BotConstants

logger = logging.getLogger(__name__)


def validate_url(url: str) -> bool:
    """Validate if the provided string is a valid URL."""
    if not url or not isinstance(url, str):
        return False
    
    try:
        result = urlparse(url.strip())
        return all([result.scheme, result.netloc])
    except Exception:
        return False


def sanitize_filename(filename: str) -> str:
    """Sanitize a string to be safe for use as a filename."""
    if not filename:
        return "unknown"
    
    # Remove or replace invalid characters
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    
    # Limit length
    if len(filename) > BotConstants.MAX_FILENAME_LENGTH:
        filename = filename[:BotConstants.MAX_FILENAME_LENGTH]
    
    return filename.strip() or "unknown"


def sanitize_query(query: str) -> str:
    """Sanitize search query for safe use in URLs."""
    if not query:
        return ""
    return quote_plus(query.strip())


def normalize_text(text: str) -> str:
    """Normalize text for consistent comparison."""
    if not text:
        return ""
    return unicodedata.normalize("NFKD", text.lower()).strip()


def is_ignored_domain(url: str) -> bool:
    """Check if URL is from an ignored domain for auto-save."""
    if not url:
        return True
    
    domain = urlparse(url).netloc.lower()
    return any(ignored in domain for ignored in BotConstants.IGNORED_DOMAINS)


def create_temp_directory(prefix: Optional[str] = None) -> str:
    """Create a temporary directory with proper error handling."""
    prefix = prefix or BotConstants.TEMP_DIR_PREFIX
    try:
        temp_dir = tempfile.mkdtemp(prefix=prefix)
        logger.debug(f"Created temporary directory: {temp_dir}")
        return temp_dir
    except Exception as e:
        logger.error(f"Failed to create temporary directory: {e}")
        raise


def cleanup_temp_directory(temp_dir: str) -> bool:
    """Clean up a temporary directory with proper error handling."""
    if not temp_dir or not os.path.exists(temp_dir):
        return True
    
    try:
        shutil.rmtree(temp_dir)
        logger.debug(f"Cleaned up temporary directory: {temp_dir}")
        return True
    except Exception as e:
        logger.error(f"Failed to clean up temporary directory {temp_dir}: {e}")
        return False


def safe_temp_directory_context():
    """Context manager for safe temporary directory handling."""
    temp_dir = None
    try:
        temp_dir = create_temp_directory()
        yield temp_dir
    finally:
        if temp_dir:
            cleanup_temp_directory(temp_dir)


def extract_metadata_from_path(path: str) -> Tuple[str, str]:
    """Extract artist and album from file path."""
    try:
        parts = Path(path).parts
        # expected: /downloads/Artist/Album
        if len(parts) >= 3:
            return parts[-2], parts[-1]
        return "Unknown Artist", Path(path).name
    except Exception as e:
        logger.warning(f"Failed to extract metadata from path {path}: {e}")
        return "Unknown Artist", "Unknown Album"


def find_cover_art(path: str) -> Optional[str]:
    """Find cover art file in the given directory."""
    try:
        for root, _, files in os.walk(path):
            for file in files:
                if (file.lower().endswith(tuple(BotConstants.IMAGE_EXTENSIONS)) and 
                    "cover" in file.lower()):
                    return os.path.join(root, file)
        return None
    except Exception as e:
        logger.warning(f"Failed to find cover art in {path}: {e}")
        return None


def count_music_files(directory: str) -> int:
    """Count music files in a directory recursively."""
    try:
        count = 0
        for root, _, files in os.walk(directory):
            count += len([f for f in files if f.lower().endswith(tuple(BotConstants.AUDIO_EXTENSIONS))])
        return count
    except Exception as e:
        logger.error(f"Failed to count music files in {directory}: {e}")
        return 0


def format_duration(seconds: int) -> str:
    """Format duration in seconds to MM:SS format."""
    if seconds <= 0:
        return "0:00"
    
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    return f"{minutes}:{remaining_seconds:02d}"


def format_file_size(bytes_size: int) -> str:
    """Format file size in bytes to human readable format."""
    if bytes_size <= 0:
        return "0 B"
    
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} TB"


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate text to specified length with suffix."""
    if not text or len(text) <= max_length:
        return text
    
    return text[:max_length - len(suffix)] + suffix


def parse_discord_mention(mention: str) -> Optional[int]:
    """Parse Discord mention and return user ID."""
    if not mention:
        return None
    
    # Match <@!123456789> or <@123456789>
    match = re.match(r'<@!?(\d+)>', mention)
    if match:
        return int(match.group(1))
    
    # Try to parse as just a number
    try:
        return int(mention)
    except ValueError:
        return None


def validate_discord_id(discord_id: str) -> bool:
    """Validate if a string is a valid Discord ID."""
    try:
        user_id = int(discord_id)
        return 17 <= len(str(user_id)) <= 20
    except (ValueError, TypeError):
        return False


def safe_int(value, default: int = 0) -> int:
    """Safely convert value to integer with default fallback."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_float(value, default: float = 0.0) -> float:
    """Safely convert value to float with default fallback."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def chunk_list(lst: List, chunk_size: int) -> List[List]:
    """Split a list into chunks of specified size."""
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]


def remove_duplicates_preserve_order(lst: List) -> List:
    """Remove duplicates from list while preserving order."""
    seen = set()
    return [x for x in lst if not (x in seen or seen.add(x))]


def is_valid_audio_file(filename: str) -> bool:
    """Check if filename has a valid audio extension."""
    if not filename:
        return False
    
    return filename.lower().endswith(tuple(BotConstants.AUDIO_EXTENSIONS))


def is_valid_image_file(filename: str) -> bool:
    """Check if filename has a valid image extension."""
    if not filename:
        return False
    
    return filename.lower().endswith(tuple(BotConstants.IMAGE_EXTENSIONS))
