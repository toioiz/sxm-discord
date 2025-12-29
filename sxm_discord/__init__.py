"""
SXM Discord Bot - Improved Version

A Discord bot that plays SiriusXM radio stations with proper memory management
and modern discord.py 2.x support.
"""

__version__ = "0.3.0"

from .player import DiscordPlayer

__all__ = ["DiscordPlayer", "__version__"]
