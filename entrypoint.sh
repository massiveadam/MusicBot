#!/bin/bash
set -e

echo "🎵 Starting Discord Music Bot..."

# Initialize beets library if it doesn't exist
if [ ! -f /app/.beets/musiclibrary.db ]; then
    echo "📚 Initializing beets library..."
    beet
fi

# Start beets web interface in background (optional)
if [ "${BEETS_WEB_ENABLED:-false}" = "true" ]; then
    echo "🌐 Starting beets web interface..."
    beet web &
fi

# Start the Discord bot
echo "🤖 Starting Discord bot..."
exec python bot.py