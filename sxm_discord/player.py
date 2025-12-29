"""
SXM Player integration for Discord bot.

This module provides the DiscordPlayer class that integrates with
the sxm-player framework to run the Discord bot.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple, Type

import click
from sxm_player.models import PlayerState
from sxm_player.players import BasePlayer, Option
from sxm_player.runner import Runner
from sxm_player.workers import BaseWorker

from .utils import set_root_command


class DiscordPlayer(BasePlayer):
    """
    SXM Player plugin for Discord integration.
    
    This class is discovered by sxm-player and provides the configuration
    and worker initialization for the Discord bot.
    """

    params: List[click.Parameter] = [
        Option(
            "--token",
            required=True,
            type=str,
            help="Discord bot token",
            envvar="SXM_DISCORD_TOKEN",
        ),
        Option(
            "--root-command",
            type=str,
            default="music",
            help="Root slash command name",
            envvar="SXM_DISCORD_ROOT_COMMAND",
        ),
        Option(
            "--description",
            type=str,
            default="SXM radio bot for Discord",
            help="Bot description in Discord",
        ),
        Option(
            "--output-channel-id",
            type=int,
            help="Discord channel ID for status messages",
            envvar="SXM_DISCORD_OUTPUT_CHANNEL",
        ),
    ]

    @staticmethod
    def get_params() -> List[click.Parameter]:
        """Return CLI parameters for this player."""
        return DiscordPlayer.params

    @staticmethod
    def get_worker_args(
        runner: Runner,
        state: PlayerState,
        **kwargs
    ) -> Optional[Tuple[Type[BaseWorker], str, dict]]:
        """
        Configure and return the worker class with arguments.
        
        This is called by sxm-player to get the worker that will
        actually run the Discord bot.
        """
        context = click.get_current_context()

        # Determine processed folder path
        processed_folder: Optional[str] = None
        if "output_folder" in kwargs and kwargs["output_folder"] is not None:
            processed_folder = os.path.join(kwargs["output_folder"], "processed")

        # Build worker parameters
        params = {
            "token": context.meta["token"],
            "description": context.meta["description"],
            "output_channel_id": context.meta["output_channel_id"],
            "processed_folder": processed_folder,
            "sxm_status": state.sxm_running,
            "stream_data": state.stream_data,
            "channels": state.get_raw_channels(),
            "raw_live_data": state.get_raw_live(),
            "root_command": context.meta["root_command"],
        }

        # Set root command for utils module
        set_root_command(context.meta["root_command"])

        # Import workers here to ensure root_command is set first
        from .bot import DiscordArchivedWorker, DiscordWorker

        # Use archived worker if we have processed content
        if processed_folder is not None:
            return (DiscordArchivedWorker, "discord", params)

        return (DiscordWorker, "discord", params)
