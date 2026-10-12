"""booking_agent_cli: typer-based stateful CLI for USP booking flows."""

from pathlib import Path

from booking_agent import settings as _settings

__version__ = "0.1.0"

CLI_DEFAULTS = Path(__file__).with_name("defaults.env")
_settings.register_defaults(CLI_DEFAULTS)
