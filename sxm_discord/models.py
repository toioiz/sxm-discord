"""
Data models for SXM Discord bot with proper memory management.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Generic, List, Optional, Tuple, TypeVar, Union
from weakref import WeakValueDictionary

import discord
from discord import Embed, FFmpegOpusAudio, Message
from humanize import naturaltime
from sxm.models import XMChannel, XMCutMarker, XMLiveChannel, XMSong

if TYPE_CHECKING:
    from sxm_player.models import Episode, PlayerState, Song


logger = logging.getLogger(__name__)


class PlayType(Enum):
    """Types of audio playback."""
    FILE = auto()
    LIVE = auto()
    RANDOM = auto()


@dataclass
class QueuedItem:
    """An item in the audio playback queue."""
    audio_file: Optional[Union[Song, Episode]] = None
    stream_data: Optional[Tuple[XMChannel, str]] = None
    source: Optional[FFmpegOpusAudio] = None
    created_at: float = field(default_factory=time.monotonic)

    def cleanup(self) -> None:
        """Clean up FFmpeg source to prevent memory leaks."""
        if self.source is not None:
            try:
                self.source.cleanup()
            except (ProcessLookupError, OSError) as e:
                logger.debug(f"Error cleaning up audio source: {e}")
            finally:
                self.source = None

    def __del__(self):
        """Ensure cleanup on garbage collection."""
        self.cleanup()


@dataclass
class ArchivedQueuedItem(QueuedItem):
    """A queued item for archived (file-based) playback."""
    audio_file: Union[Song, Episode] = field(default=None)  # type: ignore


@dataclass
class SXMQueuedItem(QueuedItem):
    """A queued item for live SXM stream playback."""
    stream_data: Tuple[XMChannel, str] = field(default=None)  # type: ignore


class SongActivity(discord.Activity):
    """Discord activity showing current song information."""

    def __init__(self, song: Optional[Song] = None, **kwargs):
        super().__init__(type=discord.ActivityType.listening, **kwargs)
        self.update_status(song)

    def update_status(
        self,
        song: Optional[Song],
        state: str = "Playing music",
        name_suffix: str = ""
    ) -> None:
        """Update activity with current song information."""
        self.state = state

        if song is not None:
            self.name = f"{song.pretty_name}{name_suffix}"
            self.details = song.pretty_name
            if hasattr(song, 'image_url') and song.image_url:
                self.large_image_url = song.image_url
            if hasattr(song, 'album') and song.album:
                self.large_image_text = f"{song.album} by {song.artist}"
        else:
            self.name = name_suffix if name_suffix else "Music"
            self.details = None


class SXMActivity(SongActivity):
    """Discord activity for live SXM radio playback."""

    def __init__(
        self,
        start: Optional[datetime],
        radio_time: Optional[datetime],
        channel: XMChannel,
        live_channel: XMLiveChannel,
        **kwargs
    ):
        # Set timestamps
        if start is not None:
            kwargs['start'] = start

        suffix = f"SXM {channel.pretty_name}"
        song = self._create_song(channel, live_channel, radio_time)

        if song is None:
            episode = live_channel.get_latest_episode(now=radio_time)
            if episode is not None:
                suffix = f'"{episode.episode.long_title}" on {suffix}'
        else:
            suffix = f" on {suffix}"

        super().__init__(song, **kwargs)
        self.update_status(song, state="Playing music from SXM", name_suffix=suffix)

    @staticmethod
    def _create_song(
        channel: XMChannel,
        live_channel: XMLiveChannel,
        radio_time: Optional[datetime]
    ) -> Optional[Song]:
        """Create a Song object from live channel data."""
        from sxm_player.models import Song
        from .utils import get_art_url_by_size

        latest_cut = live_channel.get_latest_cut(now=radio_time)
        if latest_cut is not None and isinstance(latest_cut.cut, XMSong):
            image_url = None
            if latest_cut.cut.album is not None:
                image_url = get_art_url_by_size(latest_cut.cut.album.arts, "MEDIUM")

            return Song(
                guid="",
                title=latest_cut.cut.title,
                artist=latest_cut.cut.artists[0].name,
                air_time=latest_cut.time,
                channel=channel.id,
                file_path="",
                image_url=image_url,
            )
        return None


# Type variable for carousel items
T = TypeVar('T')


class CarouselManager:
    """
    Manages reaction carousels with automatic cleanup to prevent memory leaks.
    
    Uses WeakValueDictionary combined with periodic cleanup to ensure
    carousels don't accumulate indefinitely in memory.
    """

    # Class-level timeout for carousel expiration (seconds)
    CAROUSEL_TIMEOUT = 300  # 5 minutes

    def __init__(self):
        self._carousels: dict[int, ReactionCarousel] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start the carousel cleanup background task."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """Stop the carousel cleanup task."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._carousels.clear()

    def add(self, message_id: int, carousel: ReactionCarousel) -> None:
        """Add a carousel for a message."""
        self._carousels[message_id] = carousel

    def get(self, message_id: int) -> Optional[ReactionCarousel]:
        """Get a carousel by message ID."""
        carousel = self._carousels.get(message_id)
        if carousel and carousel.is_expired(self.CAROUSEL_TIMEOUT):
            del self._carousels[message_id]
            return None
        return carousel

    def remove(self, message_id: int) -> None:
        """Remove a carousel."""
        self._carousels.pop(message_id, None)

    async def _cleanup_loop(self) -> None:
        """Background task to clean up expired carousels."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute
                self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in carousel cleanup: {e}")

    def _cleanup_expired(self) -> None:
        """Remove expired carousels."""
        expired = [
            msg_id for msg_id, carousel in self._carousels.items()
            if carousel.is_expired(self.CAROUSEL_TIMEOUT)
        ]
        for msg_id in expired:
            logger.debug(f"Removing expired carousel for message {msg_id}")
            del self._carousels[msg_id]

    @property
    def count(self) -> int:
        """Number of active carousels."""
        return len(self._carousels)


@dataclass
class ReactionCarousel(Generic[T]):
    """
    Base class for paginated embed displays with reaction navigation.
    
    Tracks creation time for automatic expiration cleanup.
    """
    items: List[T]
    index: int = 0
    message: Optional[Message] = None
    created_at: float = field(default_factory=time.monotonic)
    last_interaction: float = field(default_factory=time.monotonic)

    @property
    def current(self) -> T:
        """Get the current item."""
        return self.items[self.index]

    def is_expired(self, timeout: float) -> bool:
        """Check if this carousel has expired."""
        return (time.monotonic() - self.last_interaction) > timeout

    def touch(self) -> None:
        """Update last interaction time."""
        self.last_interaction = time.monotonic()

    def get_message_kwargs(self, state: PlayerState) -> dict:
        """Get kwargs for message creation/update. Override in subclasses."""
        raise NotImplementedError()

    async def update_message(
        self,
        content: Optional[str] = None,
        embed: Optional[Embed] = None
    ) -> None:
        """Update the carousel message."""
        if self.message is not None:
            try:
                await self.message.edit(content=content, embed=embed)
            except discord.NotFound:
                self.message = None
            except discord.HTTPException as e:
                logger.warning(f"Failed to update carousel message: {e}")

    async def clear_reactions(self) -> None:
        """Clear all reactions from the message."""
        if self.message is None:
            return

        try:
            await self.message.clear_reactions()
        except discord.Forbidden:
            # Try clearing individual reactions if we can't clear all
            for reaction in self.message.reactions:
                try:
                    await reaction.clear()
                except discord.HTTPException:
                    pass
        except discord.NotFound:
            self.message = None
        except discord.HTTPException as e:
            logger.warning(f"Failed to clear reactions: {e}")

    async def handle_reaction(self, state: PlayerState, emoji: str) -> bool:
        """
        Handle a reaction and update the carousel.
        
        Returns True if the reaction was handled.
        """
        self.touch()

        if emoji == "⬅️" and self.index > 0:
            self.index -= 1
        elif emoji == "➡️" and self.index < len(self.items) - 1:
            self.index += 1
        else:
            return False

        await self.update(state)
        return True

    async def update(
        self,
        state: PlayerState,
        interaction: Optional[discord.Interaction] = None
    ) -> None:
        """Update the carousel display."""
        kwargs = self.get_message_kwargs(state)

        if self.message is None and interaction is not None:
            # Send initial message
            await interaction.response.send_message(**kwargs)
            self.message = await interaction.original_response()
        elif self.message is not None:
            await self.update_message(**kwargs)

        if self.message is None:
            return

        # Update navigation reactions
        await self.clear_reactions()

        if len(self.items) > 1:
            if self.index > 0:
                await self.message.add_reaction("⬅️")
            if self.index < len(self.items) - 1:
                await self.message.add_reaction("➡️")


@dataclass
class SXMCutCarousel(ReactionCarousel[XMCutMarker]):
    """Carousel for displaying recent SXM song cuts."""
    latest: XMCutMarker = field(default=None)  # type: ignore
    channel: XMChannel = field(default=None)  # type: ignore
    body: str = ""

    def _get_footer(self, state: PlayerState) -> str:
        if self.current == self.latest:
            return f"Now Playing | {self.index + 1}/{len(self.items)} Recent Songs"

        now = state.radio_time or datetime.now(timezone.utc)
        time_string = naturaltime(now - self.current.time)

        return f"About {time_string} ago | {self.index + 1}/{len(self.items)} Recent Songs"

    def get_message_kwargs(self, state: PlayerState) -> dict:
        from .utils import generate_embed_from_cut

        if state.live is None:
            raise ValueError("Nothing is playing")

        episode = state.live.get_latest_episode(self.latest.time)

        return {
            "content": self.body,
            "embed": generate_embed_from_cut(
                self.channel,
                self.current,
                episode,
                footer=self._get_footer(state),
            ),
        }


@dataclass
class ArchivedSongCarousel(ReactionCarousel[Union[Song, Episode]]):
    """Carousel for displaying archived songs/episodes."""
    body: str = ""

    def _get_footer(self) -> str:
        if hasattr(self.current, 'guid'):
            return f"GUID: {self.current.guid} | {self.index + 1}/{len(self.items)} Songs"
        return f"{self.index + 1}/{len(self.items)} Songs"

    def get_message_kwargs(self, state: PlayerState) -> dict:
        from .utils import generate_embed_from_archived

        return {
            "content": self.body,
            "embed": generate_embed_from_archived(
                self.current,
                footer=self._get_footer()
            ),
        }


@dataclass
class UpcomingSongCarousel(ArchivedSongCarousel):
    """Carousel for displaying upcoming songs in the queue."""
    latest: Optional[Union[Song, Episode]] = None

    def _get_footer(self) -> str:
        if self.current == self.latest:
            message = "Playing Next"
        else:
            message = f"{self.index + 1} Away"

        return f"{message} | {self.index + 1}/{len(self.items)} Songs"
