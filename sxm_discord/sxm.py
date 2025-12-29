"""
SXM-specific commands for the Discord bot.

Provides commands for:
- Playing live SXM channels
- Searching archived content
- Creating random playlists
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, List, Optional, Tuple, Union

import discord
from discord import app_commands
from sqlalchemy import or_
from sxm.models import XMChannel
from tabulate import tabulate

from .models import ArchivedSongCarousel, PlayType
from .utils import get_root_command

if TYPE_CHECKING:
    from sxm_player.models import DBEpisode, DBSong, Episode, PlayerState, Song
    from .music import AudioPlayer


logger = logging.getLogger(__name__)


class SXMCommandsMixin:
    """
    Mixin providing SXM-related slash commands.
    
    This mixin expects the following attributes on the class:
    - player: AudioPlayer
    - _state: PlayerState
    - _pending: Optional[Tuple[XMChannel, VoiceChannel]]
    - _log: logging.Logger
    """

    player: AudioPlayer
    _state: PlayerState
    _pending: Optional[Tuple[XMChannel, discord.VoiceChannel]]
    _log: logging.Logger

    async def _summon(self, interaction: discord.Interaction) -> bool:
        """Summon the bot to user's voice channel."""
        raise NotImplementedError()

    async def create_carousel(
        self,
        interaction: discord.Interaction,
        carousel
    ) -> None:
        """Create a reaction carousel."""
        raise NotImplementedError()

    async def _play_file(
        self,
        interaction: discord.Interaction,
        item: Union[Song, Episode],
        message: bool = True,
    ) -> None:
        """Queue a file for playback."""
        raise NotImplementedError()

    def _parse_channel(
        self,
        channel_input: str
    ) -> Optional[XMChannel]:
        """Parse a channel string to XMChannel."""
        channel_input = channel_input.strip().lower()

        for channel in self._state.channels:
            # Match by ID
            if channel.id.lower() == channel_input:
                return channel
            # Match by number
            if channel.channel_number == channel_input:
                return channel
            # Match by name (partial)
            if channel_input in channel.name.lower():
                return channel

        return None

    def _parse_channels(
        self,
        channels_input: str
    ) -> List[XMChannel]:
        """Parse multiple channel specifications."""
        result = []
        parts = [p.strip() for p in channels_input.split(',')]

        for part in parts:
            channel = self._parse_channel(part)
            if channel:
                result.append(channel)

        return result

    @app_commands.command(
        name="sxm-channel",
        description="Play a SiriusXM channel"
    )
    @app_commands.describe(channel="Channel ID, number, or name")
    async def sxm_channel(
        self,
        interaction: discord.Interaction,
        channel: str
    ) -> None:
        """Play a specific SXM channel."""
        # Check SXM status
        if not self._state.sxm_running:
            await interaction.response.send_message(
                "SXM is not currently connected. Please wait...",
                ephemeral=True
            )
            return

        # Check voice channel
        if not await self._require_voice(interaction):
            return

        # Parse channel
        xm_channel = self._parse_channel(channel)
        if xm_channel is None:
            await interaction.response.send_message(
                f"Could not find channel: `{channel}`\n"
                f"Use `/sxm-channels` to see available channels.",
                ephemeral=True
            )
            return

        # Stop current playback if needed
        if self.player.is_playing:
            self._pending = None
            await self.player.stop(disconnect=False)
            await asyncio.sleep(0.5)
        else:
            if not await self._summon(interaction):
                return

        # Defer response for long operation
        if not interaction.response.is_done():
            await interaction.response.defer()

        try:
            self._log.info(f"Playing SXM channel: {xm_channel.id}")
            await self.player.add_live_stream(xm_channel)

            if self.player.voice:
                self._pending = (xm_channel, self.player.voice.channel)

                member = interaction.guild.get_member(interaction.user.id)
                voice_channel = member.voice.channel if member.voice else None
                channel_mention = voice_channel.mention if voice_channel else "voice"

                await interaction.followup.send(
                    f"Started playing **{xm_channel.pretty_name}** in {channel_mention}"
                )
        except Exception as e:
            self._log.error(f"Error starting stream: {e}")
            await self.player.stop()
            await interaction.followup.send("Something went wrong starting the stream.")

    @app_commands.command(
        name="sxm-channels",
        description="List available SXM channels"
    )
    async def sxm_channels(self, interaction: discord.Interaction) -> None:
        """Send a list of available channels via DM."""
        if not self._state.sxm_running:
            await interaction.response.send_message(
                "SXM is not currently connected.",
                ephemeral=True
            )
            return

        # Build channel table
        display_channels = []
        for channel in self._state.channels:
            display_channels.append((
                channel.id,
                int(channel.channel_number),
                channel.name,
                channel.short_description[:40] + "..." if len(channel.short_description) > 40 else channel.short_description
            ))

        display_channels.sort(key=lambda x: x[1])
        channel_table = tabulate(
            display_channels,
            headers=["ID", "#", "Name", "Description"]
        )

        await interaction.response.send_message(
            "Sending channel list via DM...",
            ephemeral=True
        )

        # Send via DM
        try:
            await interaction.user.send("**SXM Channels:**")

            # Split into chunks
            while channel_table:
                if len(channel_table) < 1900:
                    await interaction.user.send(f"```\n{channel_table}\n```")
                    break
                else:
                    # Find last newline within limit
                    idx = channel_table[:1900].rfind("\n")
                    await interaction.user.send(f"```\n{channel_table[:idx]}\n```")
                    channel_table = channel_table[idx + 1:]
        except discord.Forbidden:
            await interaction.followup.send(
                "Could not send DM. Please enable DMs from server members.",
                ephemeral=True
            )

    async def _require_voice(self, interaction: discord.Interaction) -> bool:
        """Check if user is in a voice channel."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True
            )
            return False

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                "You must be in a voice channel to use this command.",
                ephemeral=True
            )
            return False

        return True


class SXMArchivedCommandsMixin(SXMCommandsMixin):
    """
    Extended mixin with archived content commands.
    
    Provides playlist and search functionality.
    """

    @app_commands.command(
        name="sxm-playlist",
        description="Play random songs from archived channels"
    )
    @app_commands.describe(
        channels="Comma-separated channel IDs or numbers",
        threshold="Minimum songs required (default: 40)"
    )
    async def sxm_playlist(
        self,
        interaction: discord.Interaction,
        channels: str,
        threshold: int = 40
    ) -> None:
        """Create a random playlist from archived songs."""
        if not await self._require_voice(interaction):
            return

        xm_channels = self._parse_channels(channels)
        if not xm_channels:
            await interaction.response.send_message(
                f"No valid channels found in: `{channels}`",
                ephemeral=True
            )
            return

        if self._state.db is None:
            await interaction.response.send_message(
                "No database connection available.",
                ephemeral=True
            )
            return

        # Check song count
        from sxm_player.models import DBSong

        channel_ids = [c.id for c in xm_channels]
        unique_songs = (
            self._state.db.query(DBSong.title, DBSong.artist)
            .filter(DBSong.channel.in_(channel_ids))
            .distinct()
            .count()
        )

        if unique_songs < threshold:
            await interaction.response.send_message(
                f"Not enough archived songs ({unique_songs} < {threshold}).",
                ephemeral=True
            )
            return

        # Stop current playback
        if self.player.is_playing:
            await self.player.stop(disconnect=False)
            await asyncio.sleep(0.5)
        else:
            if not await self._summon(interaction):
                return

        await interaction.response.defer()

        try:
            await self.player.add_playlist(xm_channels, self._state.db)

            channel_names = ", ".join(c.pretty_name for c in xm_channels[:3])
            if len(xm_channels) > 3:
                channel_names += f" (+{len(xm_channels) - 3} more)"

            await interaction.followup.send(
                f"Started random playlist from **{channel_names}**"
            )
        except Exception as e:
            self._log.error(f"Error creating playlist: {e}")
            await self.player.stop()
            await interaction.followup.send("Failed to create playlist.")

    @app_commands.command(
        name="sxm-search",
        description="Search archived songs"
    )
    @app_commands.describe(query="Search by title or artist")
    async def sxm_search(
        self,
        interaction: discord.Interaction,
        query: str
    ) -> None:
        """Search for archived songs."""
        if self._state.db is None:
            await interaction.response.send_message(
                "No database connection available.",
                ephemeral=True
            )
            return

        from sxm_player.models import DBSong, Song

        results = (
            self._state.db.query(DBSong)
            .filter(
                or_(
                    DBSong.guid.ilike(f"{query}%"),
                    DBSong.title.ilike(f"%{query}%"),
                    DBSong.artist.ilike(f"%{query}%"),
                )
            )
            .order_by(DBSong.air_time.desc())
            .limit(10)
            .all()
        )

        if not results:
            await interaction.response.send_message(
                f"No songs found matching: `{query}`"
            )
            return

        songs = [Song.from_orm(s) for s in results]

        carousel = ArchivedSongCarousel(
            items=songs,
            body=f"Songs matching `{query}`:"
        )
        await self.create_carousel(interaction, carousel)

    @app_commands.command(
        name="sxm-play",
        description="Play a specific archived song by GUID"
    )
    @app_commands.describe(guid="Song GUID from search results")
    async def sxm_play(
        self,
        interaction: discord.Interaction,
        guid: str
    ) -> None:
        """Play a specific archived song."""
        if not await self._require_voice(interaction):
            return

        if self._state.db is None:
            await interaction.response.send_message(
                "No database connection available.",
                ephemeral=True
            )
            return

        from sxm_player.models import DBSong, Song

        db_song = self._state.db.query(DBSong).filter_by(guid=guid).first()

        if db_song is None:
            await interaction.response.send_message(
                f"Song not found: `{guid}`",
                ephemeral=True
            )
            return

        song = Song.from_orm(db_song)

        if not os.path.exists(song.file_path):
            self._log.warning(f"File not found: {song.file_path}")
            await interaction.response.send_message(
                "Song file not found on disk.",
                ephemeral=True
            )
            return

        await self._play_file(interaction, song)
