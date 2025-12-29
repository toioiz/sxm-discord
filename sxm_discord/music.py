"""
Audio player for SXM Discord bot with proper memory management.

Key improvements:
- Proper cleanup of FFmpeg processes
- Bounded queues to prevent memory growth
- Context manager for session handling
- Graceful shutdown with timeout
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections import deque
from contextlib import asynccontextmanager
from random import SystemRandom
from typing import TYPE_CHECKING, Deque, List, Optional, Tuple, Union

import discord
from discord import FFmpegOpusAudio, VoiceChannel, VoiceClient
from sqlalchemy import and_
from sqlalchemy.orm.session import Session
from sxm.models import XMChannel

from .models import (
    ArchivedQueuedItem,
    PlayType,
    QueuedItem,
    SXMQueuedItem,
)

if TYPE_CHECKING:
    from sxm_player.models import DBSong, Episode, Song
    from sxm_player.queue import EventMessage, Queue

logger = logging.getLogger(__name__)


# Constants
MAX_RECENT_ITEMS = 10
MAX_UPCOMING_ITEMS = 50
QUEUE_TIMEOUT = 5.0
VOICE_CONNECT_TIMEOUT = 30.0
CLEANUP_TIMEOUT = 10.0


class AudioPlayerError(Exception):
    """Base exception for audio player errors."""
    pass


class VoiceConnectionError(AudioPlayerError):
    """Error connecting to voice channel."""
    pass


class PlaybackError(AudioPlayerError):
    """Error during playback."""
    pass


class AudioPlayer:
    """
    Manages audio playback for the Discord bot.
    
    Key features:
    - Proper cleanup of FFmpeg processes to prevent memory leaks
    - Bounded deques for recent/upcoming items
    - Async context manager for session handling
    - Graceful shutdown with timeouts
    """

    def __init__(self, event_queue: Queue, loop: asyncio.AbstractEventLoop):
        self._event_queue = event_queue
        self._loop = loop
        self._log = logging.getLogger(__name__)
        self._random = SystemRandom()

        # Use deque with maxlen for bounded memory
        self.recent: Deque[Union[Episode, Song]] = deque(maxlen=MAX_RECENT_ITEMS)
        self.upcoming: Deque[Union[Episode, Song]] = deque(maxlen=MAX_UPCOMING_ITEMS)

        # Player state
        self.play_type: Optional[PlayType] = None
        self.repeat: bool = False

        # Async primitives
        self._player_event = asyncio.Event()
        self._player_queue: asyncio.Queue[QueuedItem] = asyncio.Queue(maxsize=100)
        self._shutdown_event = asyncio.Event()
        self._player_task: Optional[asyncio.Task] = None

        # Current state
        self._current: Optional[QueuedItem] = None
        self._playlist_data: Optional[Tuple[List[XMChannel], Session]] = None
        self._voice: Optional[VoiceClient] = None
        self._voice_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the audio player background task."""
        if self._player_task is None or self._player_task.done():
            self._shutdown_event.clear()
            self._player_task = asyncio.create_task(
                self._audio_player_loop(),
                name="audio_player"
            )

    async def stop(self, disconnect: bool = True, kill_hls: bool = True) -> None:
        """
        Stop the audio player and clean up resources.
        
        Args:
            disconnect: Whether to disconnect from voice channel
            kill_hls: Whether to kill the HLS stream
        """
        self._log.debug(f"Stopping player (disconnect={disconnect}, kill_hls={kill_hls})")

        # Clear the queue
        while not self._player_queue.empty():
            try:
                item = self._player_queue.get_nowait()
                item.cleanup()
            except asyncio.QueueEmpty:
                break

        # Clean up current item
        if self._current is not None:
            self._current.cleanup()
            self._current = None

        # Clear collections
        self.recent.clear()
        self.upcoming.clear()

        # Close playlist session
        if self._playlist_data is not None:
            try:
                self._playlist_data[1].close()
            except Exception as e:
                self._log.warning(f"Error closing playlist session: {e}")
            self._playlist_data = None

        # Handle voice client
        async with self._voice_lock:
            if self._voice is not None:
                try:
                    if self._voice.is_playing():
                        self._voice.stop()

                    if disconnect:
                        await asyncio.wait_for(
                            self._voice.disconnect(force=True),
                            timeout=CLEANUP_TIMEOUT
                        )
                        self._voice = None

                        # Kill HLS stream if playing live
                        if self.play_type == PlayType.LIVE and kill_hls:
                            from sxm_player.queue import EventMessage, EventTypes
                            self._event_queue.safe_put(
                                EventMessage("discord", EventTypes.KILL_HLS_STREAM, None)
                            )
                except asyncio.TimeoutError:
                    self._log.warning("Timeout disconnecting from voice")
                    self._voice = None
                except Exception as e:
                    self._log.error(f"Error stopping voice: {e}")
                    self._voice = None

        self.play_type = None

    async def cleanup(self) -> None:
        """Full cleanup including stopping the player task."""
        self._shutdown_event.set()
        self._player_event.set()  # Wake up the player task

        # Clean up current item
        if self._current is not None:
            self._current.cleanup()
            self._current = None

        # Wait for player task to finish
        if self._player_task and not self._player_task.done():
            try:
                await asyncio.wait_for(self._player_task, timeout=CLEANUP_TIMEOUT)
            except asyncio.TimeoutError:
                self._log.warning("Player task did not stop in time, cancelling")
                self._player_task.cancel()
                try:
                    await self._player_task
                except asyncio.CancelledError:
                    pass

    @property
    def is_playing(self) -> bool:
        """Check if currently playing audio."""
        if self._voice is None or self._current is None:
            return False
        return self._voice.is_playing()

    @property
    def voice(self) -> Optional[VoiceClient]:
        """Get the current voice client."""
        return self._voice

    @property
    def current(self) -> Optional[QueuedItem]:
        """Get the current queued item."""
        return self._current

    async def set_voice(self, channel: VoiceChannel) -> None:
        """
        Connect to or move to a voice channel.
        
        Args:
            channel: The voice channel to connect to
            
        Raises:
            VoiceConnectionError: If connection fails
        """
        async with self._voice_lock:
            try:
                if self._voice is None:
                    self._voice = await asyncio.wait_for(
                        channel.connect(),
                        timeout=VOICE_CONNECT_TIMEOUT
                    )
                elif self._voice.channel != channel:
                    await asyncio.wait_for(
                        self._voice.move_to(channel),
                        timeout=VOICE_CONNECT_TIMEOUT
                    )
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f"Timeout connecting to {channel.name}")
            except discord.ClientException as e:
                raise VoiceConnectionError(f"Failed to connect: {e}")

    async def skip(self) -> bool:
        """Skip the current track."""
        self._log.debug("Skipping track")

        if self._voice is not None:
            if self._player_queue.qsize() < 1:
                await self.stop()
            else:
                self._voice.stop()
            return True
        return False

    async def add_live_stream(
        self,
        channel: XMChannel,
        stream_url: Optional[str] = None
    ) -> bool:
        """
        Add an HLS live stream to the queue.
        
        Args:
            channel: The XM channel to play
            stream_url: Optional direct stream URL
            
        Returns:
            True if successfully added
        """
        if self.play_type is not None:
            self._log.warning(
                f"Cannot add HLS stream, already playing: {self.play_type}"
            )
            return False

        self.play_type = PlayType.LIVE
        self._log.debug(f"Adding live stream: {channel.id}")

        await self._add(stream_data=(channel, stream_url))
        return True

    async def add_playlist(
        self,
        xm_channels: List[XMChannel],
        db: Session
    ) -> bool:
        """
        Create a random playlist from archived songs.
        
        Args:
            xm_channels: Channels to pick songs from
            db: Database session (ownership transferred to player)
            
        Returns:
            True if successfully created
        """
        if self.play_type is not None:
            self._log.warning(
                f"Cannot add playlist, already playing: {self.play_type}"
            )
            return False

        self._log.debug(f"Adding playlist for channels: {[c.id for c in xm_channels]}")
        self._playlist_data = (xm_channels, db)

        # Pre-populate queue with initial songs
        for _ in range(5):
            if not await self._add_random_playlist_song():
                break

        self.play_type = PlayType.RANDOM
        return True

    async def add_file(self, file_info: Union[Song, Episode]) -> bool:
        """
        Add a file to the playback queue.
        
        Args:
            file_info: Song or Episode to play
            
        Returns:
            True if successfully added
        """
        if self.play_type == PlayType.LIVE:
            self._log.warning("Cannot add file, HLS stream is playing")
            return False

        if self.play_type is None:
            self.play_type = PlayType.FILE

        self._log.debug(f"Adding file: {file_info.file_path}")
        await self._add(file_info=file_info)
        return True

    async def _add(
        self,
        file_info: Optional[Union[Song, Episode]] = None,
        stream_data: Optional[Tuple[XMChannel, Optional[str]]] = None,
    ) -> None:
        """Internal method to add items to the queue."""
        if self._voice is None:
            self._log.warning("Discarding item: voice client not set")
            return

        item: Optional[QueuedItem] = None

        if stream_data is None and file_info is not None:
            # File playback
            item = ArchivedQueuedItem(audio_file=file_info)
            self.upcoming.append(file_info)

        elif stream_data is not None and stream_data[1] is None:
            # Need to trigger HLS stream
            self._log.debug(f"Triggering HLS stream for channel {stream_data[0].id}")
            from sxm_player.queue import EventMessage, EventTypes

            success = self._event_queue.safe_put(
                EventMessage(
                    "discord",
                    EventTypes.TRIGGER_HLS_STREAM,
                    (stream_data[0].id, "udp"),
                )
            )
            if not success:
                self._log.warning("Failed to trigger HLS stream")
            return

        elif stream_data is not None and stream_data[1] is not None:
            # Direct stream URL
            item = SXMQueuedItem(stream_data=(stream_data[0], stream_data[1]))

        if item is not None:
            try:
                await asyncio.wait_for(
                    self._player_queue.put(item),
                    timeout=QUEUE_TIMEOUT
                )
            except asyncio.TimeoutError:
                self._log.warning("Queue full, discarding item")
                item.cleanup()

    async def _add_random_playlist_song(self) -> bool:
        """Add a random song from the playlist channels."""
        if self._playlist_data is None:
            self._log.warning("Playlist data missing")
            return False

        from sxm_player.models import DBSong, Song

        try:
            channel_ids = [x.id for x in self._playlist_data[0]]
            session = self._playlist_data[1]

            # Get unique song identifiers
            song_query = (
                session.query(DBSong.title, DBSong.artist)
                .filter(DBSong.channel.in_(channel_ids))
            )
            songs = song_query.distinct().all()

            if not songs:
                self._log.warning("No songs found in playlist channels")
                return False

            # Pick a random song
            song_tuple = self._random.choice(songs)
            song = (
                session.query(DBSong)
                .filter(
                    and_(
                        DBSong.channel.in_(channel_ids),
                        DBSong.title == song_tuple[0],
                        DBSong.artist == song_tuple[1],
                    )
                )
                .first()
            )

            if song is None:
                return False

            return await self.add_file(file_info=Song.from_orm(song))

        except Exception as e:
            self._log.error(f"Error adding random song: {e}")
            return False

    async def _audio_player_loop(self) -> None:
        """Main audio player loop with error recovery."""
        while not self._shutdown_event.is_set():
            try:
                await self._audio_player_iteration()
            except asyncio.CancelledError:
                break
            except Exception:
                self._log.exception("Error in audio player loop")
                await asyncio.sleep(1)

    async def _audio_player_iteration(self) -> None:
        """Single iteration of the audio player."""
        self._player_event.clear()

        # Wait for next item with timeout
        try:
            self._current = await asyncio.wait_for(
                self._player_queue.get(),
                timeout=1.0
            )
        except asyncio.TimeoutError:
            return

        self._log.debug(f"Processing queued item: {self._current}")

        # Validate state
        if self._shutdown_event.is_set():
            if self._current:
                self._current.cleanup()
            return

        if self._voice is None:
            self._log.warning("Discarding item: no voice channel")
            self._discard()
            return

        if self.play_type is None or self._current is None:
            self._discard()
            return

        # Create audio source
        source: Optional[FFmpegOpusAudio] = None

        try:
            if self.play_type == PlayType.LIVE:
                source = await self._create_live_source()
            else:
                source = await self._create_file_source()

            if source is None:
                self._discard()
                return

            self._current.source = source

            # Start playback
            log_item = (
                self._current.stream_data[0].id
                if self._current.stream_data
                else self._current.audio_file.file_path
            )
            self._log.info(f"Playing: {log_item}")

            self._voice.play(
                self._current.source,
                after=self._on_track_end
            )

            # Wait for track to finish
            await self._player_event.wait()

            # Handle repeat/playlist continuation
            await self._handle_track_end()

        except Exception as e:
            self._log.error(f"Playback error: {e}")
            self._log.debug(traceback.format_exc())
        finally:
            if self._current:
                self._current.cleanup()
            self._current = None

    async def _create_live_source(self) -> Optional[FFmpegOpusAudio]:
        """Create audio source for live stream."""
        if self._current.audio_file is not None:
            self._log.warning("Invalid item for live playback")
            return None

        if self._current.stream_data is None:
            self._log.warning("Missing stream data")
            return None

        return FFmpegOpusAudio(
            self._current.stream_data[1],
            before_options="-f mpegts",
            options="-loglevel fatal",
        )

    async def _create_file_source(self) -> Optional[FFmpegOpusAudio]:
        """Create audio source for file playback."""
        if self._current.stream_data is not None:
            self._log.warning("Invalid item for file playback")
            return None

        if self._current.audio_file is None:
            self._log.warning("Missing audio file")
            return None

        # Update recent/upcoming lists
        if self.upcoming and self.upcoming[0] == self._current.audio_file:
            self.upcoming.popleft()
        self.recent.appendleft(self._current.audio_file)

        return FFmpegOpusAudio(self._current.audio_file.file_path)

    async def _handle_track_end(self) -> None:
        """Handle actions after a track finishes."""
        if self._shutdown_event.is_set():
            return

        # Add more songs for random playlist
        if self.play_type == PlayType.RANDOM and self._player_queue.qsize() < 5:
            await self._add_random_playlist_song()

        # Handle repeat
        elif self.repeat and self.play_type == PlayType.FILE:
            if self._current and self._current.audio_file:
                try:
                    await self._add(file_info=self._current.audio_file)
                except Exception:
                    self._log.exception("Error re-adding song for repeat")

    def _discard(self) -> None:
        """Discard current item and reset state."""
        if self._current:
            self._current.cleanup()
        self._current = None

    def _on_track_end(self, error: Optional[Exception] = None) -> None:
        """Callback when a track finishes (called from voice thread)."""
        if error:
            self._log.error(f"Track playback error: {error}")

        self._log.debug("Track ended")

        # Schedule event set on the event loop (thread-safe)
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._player_event.set)
