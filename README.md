# Discord Music Bot with GAMDL & Beets

A comprehensive Discord bot that downloads music from Apple Music and other streaming platforms, automatically imports it into your Plex library using beets, and provides rich music discovery features.

## 🆕 Recent Improvements (v2.0)

- ✅ **Centralized Configuration**: All hardcoded values moved to `config_constants.py`
- ✅ **Improved Logging**: Standardized logging throughout the codebase
- ✅ **Better Resource Management**: Proper cleanup of temporary files and connections
- ✅ **Enhanced Error Handling**: Comprehensive error handling with retry mechanisms
- ✅ **Code Organization**: Modular structure with utility functions
- ✅ **Input Validation**: Robust validation for all user inputs
- ✅ **Performance Optimizations**: Better async handling and resource management

## ✨ Features

- **Universal Music Downloads**: Download from Apple Music, Spotify, TIDAL, and more using GAMDL
- **Automatic Library Management**: Beets integration for proper tagging and organization
- **Plex Integration**: Seamlessly adds downloads to your Plex library
- **Music Discovery**: Hot updates from Pitchfork, Album of the Year, The Quietus, Bandcamp, and BrooklynVegan
- **Smart Album Recommendations**: Personal recommendation channels for users
- **React-based Interface**: Emoji reactions for quick actions
- **Bulk Operations**: Download multiple albums at once

## 🚀 Quick Start

### Option 1: Pull from GitHub Container Registry (Recommended)

```bash
# Create your environment file
cp .env.example .env
# Edit .env with your tokens

# Run with docker-compose
docker-compose up -d
```

### Option 2: Build from Source

```bash
# Clone the repository
git clone https://github.com/yourusername/discord-musicbot.git
cd discord-musicbot

# Build and run
docker-compose up --build -d
```

## 📋 Requirements

- Docker & Docker Compose
- Discord Bot Token
- Apple Music cookies (for downloads)
- Plex server (optional, for library integration)

## ⚙️ Configuration

### Environment Variables

Create a `.env` file and configure:

**New in v2.0**: All configuration values are now centralized and configurable via environment variables:

```env
# Required
DISCORD_TOKEN=your_discord_bot_token_here
DEV_GUILD_ID=your_server_id_here

# Plex Integration (Optional)
PLEX_TOKEN=your_plex_token_here
PLEX_URL=http://your-plex-server:32400

# File Paths (Optional - defaults provided)
MUSIC_FOLDER=/music
DOWNLOADS_FOLDER=/downloads
COOKIES_PATH=/app/cookies.txt

# Download Configuration (Optional)
GAMDL_CODEC=aac-legacy
DOWNLOAD_TIMEOUT=300
RETRY_ATTEMPTS=3

# Voice Connection Settings (Optional)
MAX_ROOM_PARTICIPANTS=5
VOICE_CONNECT_TIMEOUT=45
VOICE_RETRY_ATTEMPTS=8
VOICE_BASE_DELAY=2.0
VOICE_MAX_DELAY=32.0

# Audio Quality Settings (Optional)
AUDIO_BITRATE_BPS=96000
AUDIO_SAMPLE_RATE=48000
AUDIO_CHANNELS=2

# Search and Matching (Optional)
FUZZY_MATCH_THRESHOLD=60
MAX_SEARCH_RESULTS=10
MAX_AUTOCOMPLETE_RESULTS=10

# Scrobbling Settings (Optional)
MIN_SCROBBLE_TIME=30
SCROBBLE_PERCENTAGE=0.5

# Channel Names (Optional)
MUSIC_TOWN_CHANNEL=music-town
HOT_UPDATES_CHANNEL=hot-updates
LISTEN_LATER_PREFIX=listen-later-

# Beets Web Interface (Optional)
BEETS_WEB_ENABLED=true

# Last.fm Scrobbling (Optional)
LASTFM_API_KEY=your_lastfm_api_key_here
LASTFM_API_SECRET=your_lastfm_api_secret_here
```

### Apple Music Cookies

1. Log into music.apple.com in your browser
2. Export cookies to `cookies.txt` (Netscape format)
3. Place in your project directory

### Volume Mounts

```yaml
volumes:
  - /your/music/library:/music          # Your music library
  - /your/downloads:/downloads          # Temporary downloads
  - ./cookies.txt:/app/cookies.txt      # Apple Music cookies
```

## 🎵 Commands

### Core Commands

- `/rip <url>` - Download album from any music source
- `/save <url>` - Save album for later listening
- `/library <query>` - Search your Plex library
- `/ripbulk <links>` - Batch download multiple albums
- `/import [autotag] [path]` - **NEW:** Manually import files with optional auto-tagging
- `/hotupdates` - Post trending albums
- `/testembed <url>` - Preview album embed

### Reaction Controls

React to album posts with:
- 📥 Download album
- 📌 Save to personal channel
- 🎧 Mark as listened
- 🔁 Recommend to others
- ❌ Delete post

## 🔄 Automated Features

### Hot Updates Schedule
Automatically posts trending albums on:
- **Wednesdays** at 12:30 PM EST
- **Fridays** at 12:30 PM EST

### Auto-Save Links
Any music links posted in `#music-town` are automatically saved with reaction controls.

### Manual Import Options
**For files you manually place in `/downloads`:**
- `/import` - Quick import without auto-tagging (moves files as-is)
- `/import autotag:True` - Full import with auto-tagging for metadata correction
- `/import path:/custom/folder` - Import from a specific directory
- `/import autotag:True path:/custom/folder` - Full auto-tag import from custom path

**Use Cases:**
- Downloaded files from other sources
- Files with incorrect/missing metadata
- Batch processing of existing music collections

## 🏗️ Architecture

### Code Structure (v2.0)
```
/app/
├── bot.py                 # Main bot code
├── config_constants.py    # Centralized configuration
├── utils.py              # Utility functions
├── error_handling.py     # Error handling utilities
├── universal_scraper.py  # Metadata extraction
├── beets-config.yaml     # Beets configuration
├── cookies.txt           # Apple Music cookies
└── .beets/               # Beets database
```

### Download Process
1. **URL Resolution**: Converts any music URL to Apple Music via Odesli API
2. **Download**: GAMDL with fallback codec support (ALAC → AAC → etc.)
3. **Import**: Beets organizes and tags files
4. **Integration**: Appears in Plex library
5. **Notification**: Discord embed with Plexamp link

### Error Handling (v2.0)
- **Retry Mechanisms**: Automatic retry with exponential backoff
- **Resource Cleanup**: Proper cleanup of temporary files and connections
- **Graceful Degradation**: Fallback options when primary methods fail
- **User-Friendly Messages**: Clear error messages for users

### Beets Configuration
**Flexible Import Modes:**
- **Auto Mode (Default):** Files from `/rip` command use `-A` flag (no auto-tagging, just move files)
- **Manual Mode:** Use `/import autotag:True` for full auto-tagging and metadata correction
- **Custom Path:** Import from any directory with `/import path:/custom/path`

**Automatic Features:**
- ✅ ReplayGain analysis
- ✅ Album art embedding
- ✅ Genre tagging
- ✅ Duplicate detection
- ✅ Extra file handling

## 📁 Directory Structure

```
/app/
├── bot.py                 # Main bot code
├── universal_scraper.py   # Metadata extraction
├── beets-config.yaml      # Beets configuration
├── cookies.txt           # Apple Music cookies
└── .beets/               # Beets database

/music/                   # Organized music library
└── Artist/
    └── Album/
        ├── track01.flac
        └── cover.jpg

/downloads/               # Temporary downloads
```

## 🐳 Docker Details

### Multi-stage Build
- **System deps**: ffmpeg, curl, build tools
- **Python deps**: Discord.py, beets, gamdl
- **Bento4**: mp4decrypt for Apple Music decryption
- **Playwright**: For web scraping

### Health Checks
The container includes health monitoring and automatic restarts.

## 🔧 Development

### Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Install additional tools
playwright install chromium

# Run locally
python bot.py
```

### GitHub Actions
Automatic Docker builds on:
- Push to main branch
- New tags (v*)
- Pull requests

Images are published to `ghcr.io/yourusername/discord-musicbot`

## 🎛️ Advanced Configuration

### Custom Beets Plugins
Modify `beets-config.yaml` to enable additional plugins:
```yaml
plugins: web lastgenre scrub replaygain embedart extrafiles fetchart duplicates acousticbrainz
```

### Codec Preferences
GAMDL tries codecs in this order:
1. ALAC (lossless)
2. AAC Legacy
3. AAC
4. AAC HE Legacy
5. AAC HE

### Plex Integration
The bot automatically:
- Detects existing albums in Plex
- Generates Plexamp deep links
- Updates library after imports

## 🛠️ Troubleshooting

### Common Issues

**Downloads Fail**
- Check cookies.txt format and expiration
- Verify Apple Music subscription is active
- Check logs for detailed error messages

**Beets Import Errors**
- Check file permissions on `/music` directory
- Review beets logs: `docker logs discord-musicbot`
- Verify import path exists and contains files

**Plex Not Showing Albums**
- Verify Plex token and URL
- Check library scan settings
- Albums may take 2-3 minutes to appear

**Voice Connection Issues**
- Check bot permissions in Discord
- Verify voice channel settings
- Use `/debug` and `/reconnect` commands

**Command Sync Issues**
- Use `/sync` command as bot owner
- Check DEV_GUILD_ID in environment

### Debug Mode
Enable verbose logging:
```bash
docker-compose logs -f musicbot
```

### New Debug Commands (v2.0)
- `/debug` - Show voice connection status
- `/reconnect` - Force reconnect to voice channel
- `/test_plex` - Test Plex streaming URLs
- `/test_audio` - Test audio streaming configuration
- `/quality` - Show current audio quality settings

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test with Docker
5. Submit a pull request

## 📄 License

MIT License - see LICENSE file for details.

## 🙏 Acknowledgments

- [GAMDL](https://github.com/glomatico/gamdl) - Apple Music downloader
- [Beets](https://beets.io/) - Music library management
- [Discord.py](https://discordpy.readthedocs.io/) - Discord API wrapper
- [Odesli](https://odesli.co/) - Universal music links