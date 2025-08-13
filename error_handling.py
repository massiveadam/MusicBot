"""
Error handling utilities for the Discord Music Bot.
Centralized error handling and recovery mechanisms.
"""

import logging
import asyncio
from typing import Optional, Callable, Any
from functools import wraps
import traceback

logger = logging.getLogger(__name__)


class BotError(Exception):
    """Base exception for bot-specific errors."""
    pass


class AudioError(BotError):
    """Raised when audio processing fails."""
    pass


class VoiceConnectionError(BotError):
    """Raised when voice connection fails."""
    pass


class DownloadError(BotError):
    """Raised when music download fails."""
    pass


class PlexError(BotError):
    """Raised when Plex operations fail."""
    pass


def handle_errors(func: Callable) -> Callable:
    """Decorator to handle errors gracefully in async functions."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            raise
    return wrapper


def retry_on_error(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Decorator to retry functions on failure with exponential backoff."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    logger.warning(f"Attempt {attempt + 1}/{max_attempts} failed for {func.__name__}: {e}")
                    
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(f"All {max_attempts} attempts failed for {func.__name__}")
                        raise last_exception
            
            raise last_exception
        return wrapper
    return decorator


def safe_execute(func: Callable, *args, default: Any = None, **kwargs) -> Any:
    """Safely execute a function and return default value on error."""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.warning(f"Safe execution of {func.__name__} failed: {e}")
        return default


async def safe_async_execute(func: Callable, *args, default: Any = None, **kwargs) -> Any:
    """Safely execute an async function and return default value on error."""
    try:
        return await func(*args, **kwargs)
    except Exception as e:
        logger.warning(f"Safe async execution of {func.__name__} failed: {e}")
        return default


def cleanup_resources(resources: list) -> None:
    """Clean up a list of resources (files, connections, etc.)."""
    for resource in resources:
        try:
            if hasattr(resource, 'close'):
                resource.close()
            elif hasattr(resource, 'cleanup'):
                resource.cleanup()
            elif hasattr(resource, 'disconnect'):
                resource.disconnect()
        except Exception as e:
            logger.warning(f"Failed to cleanup resource {resource}: {e}")


class ResourceManager:
    """Context manager for managing multiple resources."""
    
    def __init__(self):
        self.resources = []
    
    def add(self, resource):
        """Add a resource to be managed."""
        self.resources.append(resource)
        return resource
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        cleanup_resources(self.resources)
        self.resources.clear()


def log_error_context(context: str, error: Exception, extra_info: Optional[dict] = None):
    """Log error with context and extra information."""
    error_msg = f"Error in {context}: {error}"
    if extra_info:
        error_msg += f" | Extra info: {extra_info}"
    
    logger.error(error_msg)
    logger.debug(f"Full traceback: {traceback.format_exc()}")


def is_retryable_error(error: Exception) -> bool:
    """Check if an error is retryable."""
    retryable_errors = (
        ConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
        OSError,
    )
    
    return isinstance(error, retryable_errors)


def get_error_summary(error: Exception) -> str:
    """Get a user-friendly error summary."""
    if isinstance(error, AudioError):
        return "Audio processing failed. Please try again."
    elif isinstance(error, VoiceConnectionError):
        return "Voice connection failed. Please check your connection and try again."
    elif isinstance(error, DownloadError):
        return "Download failed. Please check the URL and try again."
    elif isinstance(error, PlexError):
        return "Plex operation failed. Please check your Plex configuration."
    elif isinstance(error, ConnectionError):
        return "Connection failed. Please check your internet connection."
    elif isinstance(error, TimeoutError):
        return "Operation timed out. Please try again."
    else:
        return "An unexpected error occurred. Please try again."
