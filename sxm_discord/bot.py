"""
Discord bot worker for SXM streaming with proper memory management.

Key improvements over original:
- Uses modern discord.py 2.x with app_commands (slash commands)
- Proper cleanup of resources on shutdown
- CarouselManager for bounded carousel storage
- Better error handling with recovery
- Graceful shutdown handling
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

import discord
from discord import Activity, Intents, TextChannel, VoiceChannel, app_commands
from discord.ext import commands, tasks

from sxm.models import XMChannel
from sxm_player.models import Episode, PlayerState, Song
from sxm_player.queue import EventMessage, EventTypes
from sxm_player.signals import TerminateInterrupt
from sxm_player.workers import (
    HLSStatusSubscriber,
    InterruptableWorker,
    SXMStatusSubscriber,
)

from .models import (
    ArchivedSongCarousel,
    CarouselManager,
    PlayType,
    ReactionCarousel,
    SongActivity,
    SXMActivity,
    SXMCutCarousel,
    UpcomingSongCarousel,
)
from .music import AudioPlayer
from .utils import (
    generate_embed_from_archived,
    generate_now_playing_embed,
    get_recent_songs,
)

if TYPE_CHECKING:
    pass


logger = logging.getLogger(__name__)


# Constants
UPDATE_INTERVAL = 5.0
VOICE_TIMEOUT = 300  # 5 minutes of inactivity


class SXMBot(commands.Bot):
    """
    Custom bot class with proper lifecycle management.
    """

    def __init__(
        self,
        *,
        command_prefix: str,
        intents: Intents,
        description: str,
        worker: "DiscordWorker",
    ):
        super().__init__(
            command_prefix=command_prefix,
            intents=intents,
            description=description,
            help_command=None,
        )
        self.worker = worker

    async def setup_hook(self) -> None:
        """Called when the bot is starting up."""
        # Add the cog
        await self.add_cog(self.worker)

        # Sync commands globally
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")


class DiscordWorker(
    commands.Cog,
    InterruptableWorker,
    HLSStatusSubscriber,
    SXMStatusSubscriber,
):
    """
    Main Discord worker cog with SXM integration.
    
    Handles:
    - Voice channel management
    - Audio playback
    - SXM/HLS stream integration
    - Slash commands for control
    """

    def __init__(
        self,
        token: str,
        description: str,
        output_channel_id: Optional[int],
        processed_folder: str,
        sxm_status: bool,
        stream_data: Tuple[Optional[str], Optional[str]] = (None, None),
        channels: Optional[List[dict]] = None,
        raw_live_data: Tuple[
            Optional[datetime], Optional[timedelta], Optional[dict]
        ] = (None, None, None),
        root_command: str = "music",
        *args,
        **kwargs,
    ):
        # Initialize worker parents
        sxm_status_queue = kwargs.pop("sxm_status_queue")
        SXMStatusSubscriber.__init__(self, sxm_status_queue)
        hls_stream_queue = kwargs.pop("hls_stream_queue")
        HLSStatusSubscriber.__init__(self, hls_stream_queue)

        kwargs["name"] = "music"
        InterruptableWorker.__init__(self, *args, **kwargs)

        # Initialize Cog (no args needed)
        commands.Cog.__init__(self)

        # Player state
        self._state = PlayerState()
        self._state.sxm_running = sxm_status
        self._state.update_stream_data(stream_data)
        self._state.processed_folder = processed_folder
        self._state.update_channels(channels)
        self._state.set_raw_live(raw_live_data)
        self._event_queues = [self.sxm_status_queue, self.hls_stream_queue]

        # Configuration
        self.root_command = root_command
        self.token = token
        self._output_channel_id = output_channel_id
        self.output_channel: Optional[TextChannel] = None

        # Create bot with proper intents
        intents = Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True

        self.bot = SXMBot(
            command_prefix=f"/{self.root_command}",
            intents=intents,
            description=description,
            worker=self,
        )

        # These will be initialized after bot starts
        self.player: Optional[AudioPlayer] = None
        self.carousel_manager = CarouselManager()

        # State tracking
        self._last_update: float = 0
        self._pending: Optional[Tuple[XMChannel, VoiceChannel]] = None
        self._last_voice_activity: float = time.monotonic()

    def run(self) -> None:
        """Start the Discord bot."""
        self._log.info("Discord bot starting...")
        try:
            self.bot.run(self.token, log_handler=None)
        except (KeyboardInterrupt, TerminateInterrupt, RuntimeError):
            pass
        except Exception:
            self._log.exception("Bot crashed")

    async def cog_unload(self) -> None:
        """Clean up when cog is unloaded."""
        self._log.info("Shutting down Discord worker...")

        # Stop carousel manager
        await self.carousel_manager.stop()

        # Stop player
        if self.player is not None:
            await self.player.cleanup()
            await self.player.stop()

        # Send shutdown message
        await self.bot_output("Music bot shutting down")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Called when the bot is ready."""
        user = self.bot.user
        self._log.info(f"Logged in as {user} (id: {user.id})")

        # Initialize player
        self.player = AudioPlayer(self.event_queue, self.bot.loop)
        await self.player.start()

        # Start carousel manager
        await self.carousel_manager.start()

        # Find output channel
        if self._output_channel_id is not None:
            for channel in self.bot.get_all_channels():
                if channel.id == self._output_channel_id:
                    self.output_channel = channel  # type: ignore
                    break

            if self.output_channel is None:
                self._log.warning(f"Could not find output channel: {self._output_channel_id}")
            else:
                self._log.info(f"Output channel: {self.output_channel.name}")

        await self.bot_output(f"Accepting `/{self.root_command}` commands")

        if self._state.sxm_running:
            await self._sxm_running_message()

        # Start background tasks
        self.event_loop_task.start()
        self.voice_timeout_task.start()

    @commands.Cog.listener()
    async def on_reaction_add(
        self,
        reaction: discord.Reaction,
        user: Union[discord.Member, discord.User]
    ) -> None:
        """Handle reaction additions for carousels."""
        # Ignore bot reactions
        if user.id == self.bot.user.id:
            return

        carousel = self.carousel_manager.get(reaction.message.id)
        if carousel is not None:
            carousel.message = reaction.message
            await carousel.handle_reaction(self._state, str(reaction.emoji))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ) -> None:
        """Track voice state for timeout handling."""
        if self.player and self.player.voice:
            if self.player.voice.channel:
                # Check if someone joined our channel
                if after.channel == self.player.voice.channel:
                    self._last_voice_activity = time.monotonic()

    # Background Tasks
    @tasks.loop(seconds=0.1)
    async def event_loop_task(self) -> None:
        """Process events from SXM/HLS workers."""
        try:
            await self._process_events()
        except Exception:
            self._log.exception("Error in event loop")

    @event_loop_task.before_loop
    async def before_event_loop(self) -> None:
        """Wait for bot to be ready."""
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def voice_timeout_task(self) -> None:
        """Check for voice channel timeout."""
        if not self.player or not self.player.is_playing:
            return

        if self.player.voice and self.player.voice.channel:
            # Check if we're alone in the channel
            members = [m for m in self.player.voice.channel.members if not m.bot]
            if not members:
                if time.monotonic() - self._last_voice_activity > VOICE_TIMEOUT:
                    self._log.info("Voice timeout - disconnecting")
                    await self.player.stop()
                    await self.bot_output("Disconnected due to inactivity")

    @voice_timeout_task.before_loop
    async def before_voice_timeout(self) -> None:
        """Wait for bot to be ready."""
        await self.bot.wait_until_ready()

    async def _process_events(self) -> None:
        """Process SXM and HLS status events."""
        was_connected = self._state.sxm_running

        for queue in self._event_queues:
            event = queue.safe_get()
            if event:
                self._log.debug(f"Received event: {event.msg_src}, {event.msg_type.name}")
                await self._handle_event(event)

        # Handle SXM connection state changes
        if self._state.sxm_running and not was_connected:
            await self._sxm_running_message()
            if self._pending is not None:
                await self.bot_output(
                    f"Automatically resuming previous channel: `{self._pending[0].id}`"
                )
                await self._reset_live(self._pending[1], self._pending[0])
        elif not self._state.sxm_running and was_connected:
            await self.bot_output("Connection to SXM was lost. Will automatically reconnect")
            if self.player and self.player.is_playing and self.player.play_type == PlayType.LIVE:
                await self.player.stop(disconnect=False)

        # Periodic update
        if time.monotonic() > (self._last_update + UPDATE_INTERVAL):
            await self._update_activity()
            self._last_update = time.monotonic()

    async def _handle_event(self, event: EventMessage) -> None:
        """Handle a single event from the queues."""
        if event.msg_type == EventTypes.SXM_STATUS:
            # Update SXM connection status
            self._state.sxm_running = event.msg
        elif event.msg_type == EventTypes.HLS_STREAM_STARTED:
            # HLS stream is ready, start playback
            channel_id, stream_url = event.msg
            xm_channel = self._state.get_channel(channel_id)
            if xm_channel and self.player:
                await self.player.add_live_stream(xm_channel, stream_url)
        elif event.msg_type == EventTypes.UPDATE_CHANNELS:
            self._state.update_channels(event.msg)
        elif event.msg_type == EventTypes.UPDATE_LIVE:
            self._state.set_raw_live(event.msg)

    async def _sxm_running_message(self) -> None:
        """Send message when SXM is available."""
        await self.bot_output(
            f"SXM now available for streaming. {len(self._state.channels)} channels available"
        )

    async def _update_activity(self) -> None:
        """Update bot's Discord activity status."""
        if not self.player:
            return

        activity: Optional[Activity] = None

        if self.player.play_type == PlayType.LIVE:
            if self._state.live is not None:
                xm_channel = self._state.get_channel(self._state.stream_channel)
                if xm_channel is not None:
                    activity = SXMActivity(
                        self._state.start_time,
                        self._state.radio_time,
                        xm_channel,
                        self._state.live,
                    )
        elif self.player.play_type in (PlayType.FILE, PlayType.RANDOM):
            if self.player.current and self.player.current.audio_file:
                activity = SongActivity(self.player.current.audio_file)  # type: ignore

        if activity:
            await self.bot.change_presence(activity=activity)

    async def _reset_live(
        self,
        voice_channel: VoiceChannel,
        xm_channel: XMChannel
    ) -> None:
        """Reset and restart live stream."""
        if not self.player:
            return

        await self.player.stop(kill_hls=False)
        await self.player.cleanup()

        # Create new player
        self.player = AudioPlayer(self.event_queue, self.bot.loop)
        await self.player.start()

        await asyncio.sleep(5)  # Allow time for cleanup

        await self.player.set_voice(voice_channel)
        await self.player.add_live_stream(xm_channel)

    # Helper Methods
    async def bot_output(self, message: str) -> None:
        """Send a message to the output channel."""
        self._log.info(f"Bot output: {message}")
        if self.output_channel is not None:
            try:
                await self.output_channel.send(message)
            except discord.HTTPException as e:
                self._log.warning(f"Failed to send output: {e}")

    async def _summon(self, interaction: discord.Interaction) -> bool:
        """Summon bot to user's voice channel."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True
            )
            return False

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                "You must be in a voice channel.",
                ephemeral=True
            )
            return False

        if self.player:
            await self.player.set_voice(member.voice.channel)
            self._last_voice_activity = time.monotonic()

        return True

    async def _play_file(
        self,
        interaction: discord.Interaction,
        item: Union[Song, Episode],
        message: bool = True,
    ) -> None:
        """Queue a file to be played."""
        if not self.player:
            return

        if self.player.is_playing:
            if self.player.play_type != PlayType.FILE:
                self._pending = None
                await self.player.stop(disconnect=False)
                await asyncio.sleep(0.5)
        else:
            if not await self._summon(interaction):
                return

        try:
            self._log.info(f"Playing: {item.file_path}")
            await self.player.add_file(item)
        except Exception:
            self._log.error(f"Error adding file: {traceback.format_exc()}")
            if interaction.response.is_done():
                await interaction.followup.send("Failed to add to queue")
            else:
                await interaction.response.send_message("Failed to add to queue")
        else:
            if message:
                msg = f"Added **{item.bold_name}** to now playing queue"
                if interaction.response.is_done():
                    await interaction.followup.send(msg)
                else:
                    await interaction.response.send_message(msg)

    async def create_carousel(
        self,
        interaction: discord.Interaction,
        carousel: ReactionCarousel
    ) -> None:
        """Create and register a reaction carousel."""
        await carousel.update(self._state, interaction)

        if len(carousel.items) > 1 and carousel.message is not None:
            self.carousel_manager.add(carousel.message.id, carousel)

    # Slash Commands
    @app_commands.command(name="playing", description="Show what's currently playing")
    async def playing(self, interaction: discord.Interaction) -> None:
        """Responds with what the bot is currently playing."""
        if not self.player or not self.player.is_playing:
            await interaction.response.send_message("Nothing is currently playing.")
            return

        channel = self.player.voice.channel if self.player.voice else None
        channel_mention = channel.mention if channel else "Unknown"

        if self.player.play_type == PlayType.LIVE:
            if self._state.stream_channel is None:
                await interaction.response.send_message("Live stream information unavailable.")
                return

            xm_channel, embed = generate_now_playing_embed(self._state)
            await interaction.response.send_message(
                f"Currently playing **{xm_channel.pretty_name}** on {channel_mention}",
                embed=embed
            )
        elif self.player.current and self.player.current.audio_file:
            name = self.player.current.audio_file.bold_name
            embed = generate_embed_from_archived(self.player.current.audio_file)
            await interaction.response.send_message(
                f"Currently playing {name} on {channel_mention}",
                embed=embed
            )
        else:
            await interaction.response.send_message("Unable to determine what's playing.")

    @app_commands.command(name="recent", description="Show recently played songs")
    @app_commands.describe(count="Number of songs to show (1-10)")
    async def recent(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 10] = 3
    ) -> None:
        """Show recently played songs."""
        if not self.player or not self.player.is_playing:
            await interaction.response.send_message("Nothing is currently playing.")
            return

        if self.player.play_type == PlayType.LIVE:
            if self._state.stream_channel is None:
                await interaction.response.send_message("No channel information available.")
                return

            xm_channel, song_cuts, latest_cut = get_recent_songs(self._state, count)

            if not song_cuts:
                await interaction.response.send_message("No recent songs played.")
                return

            message = (
                f"Most recent song for **{xm_channel.pretty_name}**:"
                if len(song_cuts) == 1
                else f"{len(song_cuts)} most recent songs for **{xm_channel.pretty_name}**:"
            )

            carousel = SXMCutCarousel(
                items=song_cuts,
                latest=latest_cut,
                channel=xm_channel,
                body=message,
            )
            await self.create_carousel(interaction, carousel)
        else:
            recent_list = list(self.player.recent)[:count]
            if not recent_list:
                await interaction.response.send_message("No recent songs.")
                return

            carousel = ArchivedSongCarousel(
                items=recent_list,
                body="Recent songs/shows"
            )
            await self.create_carousel(interaction, carousel)

    @app_commands.command(name="stop", description="Stop playing and leave voice channel")
    async def stop(self, interaction: discord.Interaction) -> None:
        """Stop playing and disconnect."""
        if not self.player:
            await interaction.response.send_message("Bot is not active.")
            return

        self._pending = None
        await self.player.stop()
        await interaction.response.send_message("Stopped playing music.")

    @app_commands.command(name="summon", description="Join your voice channel")
    async def summon(self, interaction: discord.Interaction) -> None:
        """Join the user's voice channel."""
        if await self._summon(interaction):
            member = interaction.guild.get_member(interaction.user.id)
            await interaction.response.send_message(
                f"Joined {member.voice.channel.mention}"
            )

    @app_commands.command(name="reset", description="Force reset the audio player")
    async def reset(self, interaction: discord.Interaction) -> None:
        """Hard reset the audio player."""
        if not await self._summon(interaction):
            return

        if self.player:
            self._pending = None
            await self.player.stop()
            await self.player.cleanup()

        # Create new player
        self.player = AudioPlayer(self.event_queue, self.bot.loop)
        await self.player.start()

        await interaction.response.send_message("Bot reset successfully.")

    @app_commands.command(name="repeat", description="Toggle repeat mode")
    @app_commands.describe(enabled="Turn repeat on or off")
    async def repeat(
        self,
        interaction: discord.Interaction,
        enabled: Optional[bool] = None
    ) -> None:
        """Set or check repeat mode."""
        if not self.player or not self.player.is_playing:
            await interaction.response.send_message("Nothing is currently playing.")
            return

        if enabled is None:
            status = "on" if self.player.repeat else "off"
            await interaction.response.send_message(f"Repeat is currently {status}.")
        elif self.player.play_type == PlayType.LIVE:
            await interaction.response.send_message(
                "Cannot change repeat while playing live SXM."
            )
        elif self.player.play_type == PlayType.RANDOM:
            await interaction.response.send_message(
                "Cannot change repeat while playing random playlist."
            )
        else:
            self.player.repeat = enabled
            status = "on" if enabled else "off"
            await interaction.response.send_message(f"Set repeat to {status}.")


class DiscordArchivedWorker(DiscordWorker):
    """Extended worker with archived content commands."""

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction) -> None:
        """Skip the current song."""
        if not self.player or not self.player.is_playing:
            await interaction.response.send_message("Nothing is playing.")
            return

        if self.player.play_type == PlayType.LIVE:
            await interaction.response.send_message("Cannot skip live SXM radio.")
            return

        await self.player.skip()
        await interaction.response.send_message("Song skipped.")

    @app_commands.command(name="upcoming", description="Show upcoming songs in queue")
    async def upcoming(self, interaction: discord.Interaction) -> None:
        """Show the upcoming song queue."""
        if not self.player or not self.player.is_playing:
            await interaction.response.send_message("Nothing is playing.")
            return

        if self.player.play_type == PlayType.LIVE:
            await interaction.response.send_message("Live radio playing - no queue.")
            return

        upcoming_list = list(self.player.upcoming)
        if not upcoming_list:
            await interaction.response.send_message("Queue is empty.")
            return

        current = self.player.current.audio_file if self.player.current else None
        carousel = UpcomingSongCarousel(
            items=upcoming_list,
            body="Upcoming songs/shows:",
            latest=current,
        )
        await self.create_carousel(interaction, carousel)
