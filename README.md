# SXM Discord Bot - Improved Version

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![discord.py 2.x](https://img.shields.io/badge/discord.py-2.x-blue.svg)](https://discordpy.readthedocs.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Discord bot that plays SiriusXM radio stations. This is an improved and modernized version of [sxm-discord](https://github.com/AngellusMortis/sxm-discord) with significant memory and stability improvements.

> ⚠️ **Warning**: Designed for PERSONAL USE ONLY. Using this in any corporate setting or to attempt to pirate music may result in legal trouble.

## Key Improvements Over Original

### 🔧 Memory Management

| Issue | Original | Improved |
|-------|----------|----------|
| Carousel storage | Unbounded dict, never cleaned | `CarouselManager` with auto-expiration |
| Recent/upcoming lists | Python lists | Bounded `deque` with `maxlen` |
| SQLAlchemy sessions | Never closed | Explicit session management |
| FFmpeg sources | Leaked on errors | `cleanup()` in `finally` blocks |
| Class-level mutables | Shared across instances | Instance-level initialization |

### 🚀 Stability Improvements

- **Modern discord.py 2.x**: Uses `app_commands` instead of deprecated `discord-py-slash-command`
- **Proper async cleanup**: All tasks have timeout-based cancellation
- **Error recovery**: Event loop continues after exceptions
- **Voice timeout**: Automatically disconnects after inactivity
- **Connection state tracking**: Handles reconnections gracefully

### 📦 Architecture Changes

```
sxm_discord/
├── __init__.py      # Package initialization
├── player.py        # sxm-player integration
├── bot.py           # Discord bot & worker (modernized)
├── music.py         # Audio player with bounded queues
├── models.py        # Data models with cleanup support
├── sxm.py           # SXM slash commands
└── utils.py         # Helper functions
```

## Installation

```bash
pip install sxm-discord
```

Or install from source:

```bash
git clone https://github.com/yourusername/sxm-discord.git
cd sxm-discord
pip install -e .
```

## Usage

```bash
sxm-player sxm_discord.DiscordPlayer \
    --token YOUR_DISCORD_BOT_TOKEN \
    --username YOUR_SXM_USERNAME \
    --password YOUR_SXM_PASSWORD \
    --root-command music \
    --output-channel-id 123456789
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `SXM_DISCORD_TOKEN` | Discord bot token |
| `SXM_DISCORD_ROOT_COMMAND` | Slash command prefix (default: `music`) |
| `SXM_DISCORD_OUTPUT_CHANNEL` | Channel for status messages |

## Slash Commands

| Command | Description |
|---------|-------------|
| `/playing` | Show currently playing |
| `/recent [count]` | Show recent songs (1-10) |
| `/stop` | Stop and disconnect |
| `/summon` | Join voice channel |
| `/reset` | Hard reset player |
| `/repeat [on\|off]` | Toggle repeat |
| `/sxm-channel <channel>` | Play SXM channel |
| `/sxm-channels` | List all channels |
| `/sxm-playlist <channels>` | Random playlist |
| `/sxm-search <query>` | Search archived songs |
| `/sxm-play <guid>` | Play archived song |

## Technical Details

### Memory-Safe Carousel System

```python
class CarouselManager:
    """
    Manages reaction carousels with automatic cleanup.
    - Carousels expire after 5 minutes of inactivity
    - Background task cleans up every 60 seconds
    - Bounded storage prevents unbounded growth
    """
    
    CAROUSEL_TIMEOUT = 300  # 5 minutes
```

### Bounded Audio Queue

```python
# Using deque with maxlen prevents memory growth
self.recent: Deque[Song] = deque(maxlen=10)
self.upcoming: Deque[Song] = deque(maxlen=50)

# Queue has max size to prevent backpressure issues
self._player_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
```

### Proper Resource Cleanup

```python
@dataclass
class QueuedItem:
    source: Optional[FFmpegOpusAudio] = None
    
    def cleanup(self) -> None:
        """Clean up FFmpeg source to prevent memory leaks."""
        if self.source is not None:
            try:
                self.source.cleanup()
            except (ProcessLookupError, OSError):
                pass
            finally:
                self.source = None
    
    def __del__(self):
        """Ensure cleanup on garbage collection."""
        self.cleanup()
```

### Voice Timeout

```python
@tasks.loop(seconds=60)
async def voice_timeout_task(self) -> None:
    """Disconnect after 5 minutes of being alone in channel."""
    if self.player.voice and self.player.voice.channel:
        members = [m for m in self.player.voice.channel.members if not m.bot]
        if not members:
            if time.monotonic() - self._last_voice_activity > 300:
                await self.player.stop()
```

## Requirements

- Python 3.10+
- discord.py 2.3+
- sxm-player 0.2.5+
- FFmpeg (system dependency)

## Migrating from Original

1. Update your dependencies:
   ```bash
   pip uninstall discord-py-slash-command
   pip install -U discord.py>=2.3.0
   ```

2. Update your bot permissions in Discord Developer Portal:
   - Enable `applications.commands` scope
   - Enable `GUILD_VOICE_STATES` intent

3. Commands now use native slash commands - they should auto-sync on startup

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests: `pytest`
5. Run linting: `ruff check .`
6. Submit a pull request

## License

MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

- Original [sxm-discord](https://github.com/AngellusMortis/sxm-discord) by AngellusMortis
- [sxm-player](https://github.com/AngellusMortis/sxm-player) framework
- [discord.py](https://discordpy.readthedocs.io/) library
