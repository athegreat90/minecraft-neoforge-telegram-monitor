"""Telegram monitor for official Minecraft Java Edition and NeoForge releases."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("minecraft-neoforge-telegram-monitor")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
