"""
Utility functions for SXM Discord bot.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional, Tuple, Union

import discord
from discord import Embed
from humanize import naturaltime
from sxm.models import XMArt, XMChannel, XMCutMarker, XMImage, XMSong

if TYPE_CHECKING:
    from sxm.models import XMLiveChannel
    from sxm_player.models import Episode, PlayerState, Song


logger = logging.getLogger(__name__)

# Module-level root command (set during startup)
_ROOT_COMMAND = "music"


def set_root_command(command: str) -> None:
    """Set the root slash command name."""
    global _ROOT_COMMAND
    _ROOT_COMMAND = command


def get_root_command() -> str:
    """Get the root slash command name."""
    return _ROOT_COMMAND


def get_art_url_by_size(
    arts: Optional[List[Union[XMArt, XMImage]]],
    size: str
) -> Optional[str]:
    """Get artwork URL by size preference."""
    if arts is None:
        return None

    for art in arts:
        if hasattr(art, 'size') and art.size == size:
            return art.url
        elif hasattr(art, 'name') and art.name == size:
            return art.url

    # Fallback to first available
    if arts:
        return arts[0].url

    return None


def create_base_embed(
    title: str,
    description: Optional[str] = None,
    color: int = 0x3498db,
    thumbnail_url: Optional[str] = None,
    footer: Optional[str] = None,
) -> Embed:
    """Create a base embed with common styling."""
    embed = Embed(
        title=title,
        description=description,
        color=color,
    )

    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    if footer:
        embed.set_footer(text=footer)

    return embed


def generate_embed_from_cut(
    channel: XMChannel,
    cut: XMCutMarker,
    episode: Optional[object] = None,
    footer: Optional[str] = None,
) -> Embed:
    """Generate an embed for an SXM song cut."""
    song = cut.cut

    if not isinstance(song, XMSong):
        # Handle non-song cuts (shows, etc.)
        title = getattr(song, 'title', 'Unknown')
        embed = create_base_embed(
            title=title,
            description=f"On {channel.pretty_name}",
            footer=footer,
        )
        return embed

    # Get artwork
    image_url = None
    if song.album and song.album.arts:
        image_url = get_art_url_by_size(song.album.arts, "MEDIUM")

    # Build description
    artist = song.artists[0].name if song.artists else "Unknown Artist"
    description_parts = [f"**{artist}**"]

    if song.album:
        description_parts.append(f"Album: {song.album.title}")

    if episode:
        episode_title = getattr(episode.episode, 'long_title', None)
        if episode_title:
            description_parts.append(f"Show: {episode_title}")

    embed = create_base_embed(
        title=song.title,
        description="\n".join(description_parts),
        thumbnail_url=image_url,
        footer=footer,
    )

    # Add channel info
    embed.add_field(
        name="Channel",
        value=f"{channel.pretty_name} (#{channel.channel_number})",
        inline=True
    )

    return embed


def generate_embed_from_archived(
    item: Union[Song, Episode],
    footer: Optional[str] = None,
) -> Embed:
    """Generate an embed for an archived song or episode."""
    from sxm_player.models import Episode, Song

    if isinstance(item, Song):
        title = item.title
        description = f"**{item.artist}**"
        if item.album:
            description += f"\nAlbum: {item.album}"
        image_url = item.image_url
    else:
        # Episode
        title = item.title
        description = f"Show: {item.show}"
        image_url = item.image_url if hasattr(item, 'image_url') else None

    embed = create_base_embed(
        title=title,
        description=description,
        thumbnail_url=image_url,
        footer=footer,
    )

    # Add metadata
    if hasattr(item, 'channel') and item.channel:
        embed.add_field(name="Channel", value=item.channel, inline=True)

    if hasattr(item, 'air_time') and item.air_time:
        time_str = naturaltime(datetime.now(timezone.utc) - item.air_time)
        embed.add_field(name="Aired", value=time_str, inline=True)

    return embed


def generate_now_playing_embed(
    state: PlayerState,
) -> Tuple[XMChannel, Embed]:
    """Generate an embed for the currently playing live channel."""
    xm_channel = state.get_channel(state.stream_channel)

    if xm_channel is None:
        raise ValueError("No channel information available")

    live = state.live
    radio_time = state.radio_time

    # Try to get current song
    embed_title = xm_channel.pretty_name
    description_parts = [f"Channel #{xm_channel.channel_number}"]
    image_url = None
    footer = None

    if live is not None:
        latest_cut = live.get_latest_cut(now=radio_time)

        if latest_cut and isinstance(latest_cut.cut, XMSong):
            song = latest_cut.cut
            embed_title = song.title
            artist = song.artists[0].name if song.artists else "Unknown"
            description_parts = [f"**{artist}**", f"on {xm_channel.pretty_name}"]

            if song.album:
                description_parts.append(f"Album: {song.album.title}")
                if song.album.arts:
                    image_url = get_art_url_by_size(song.album.arts, "MEDIUM")

            footer = "Now Playing"
        else:
            # Check for episode/show
            episode = live.get_latest_episode(now=radio_time)
            if episode:
                episode_title = getattr(episode.episode, 'long_title', 'Unknown Show')
                description_parts.append(f"Show: {episode_title}")

    embed = create_base_embed(
        title=embed_title,
        description="\n".join(description_parts),
        thumbnail_url=image_url,
        footer=footer,
    )

    return xm_channel, embed


def get_recent_songs(
    state: PlayerState,
    count: int = 3,
) -> Tuple[XMChannel, List[XMCutMarker], Optional[XMCutMarker]]:
    """Get recent songs from the live channel."""
    xm_channel = state.get_channel(state.stream_channel)

    if xm_channel is None:
        raise ValueError("No channel information available")

    song_cuts: List[XMCutMarker] = []
    latest_cut: Optional[XMCutMarker] = None

    if state.live is not None:
        radio_time = state.radio_time

        # Get all recent cuts
        for cut in state.live.song_cuts[:count + 5]:  # Get extra in case of filtering
            if isinstance(cut.cut, XMSong):
                if latest_cut is None:
                    latest_cut = cut
                song_cuts.append(cut)

                if len(song_cuts) >= count:
                    break

    return xm_channel, song_cuts, latest_cut
