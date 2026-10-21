"""Load API keys and other settings from .env files.

Variables already set in the environment always win, so a key exported in the
shell overrides the files. Among the files, the first one to define a variable
wins: an explicit --env-file, then ./.env, then the user config file.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def user_env_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "audiopub" / ".env"


def load_env(explicit: Path | None = None) -> list[Path]:
    """Load .env files without overriding existing variables; returns the files read."""
    if explicit is not None and not explicit.is_file():
        raise SystemExit(f"env file not found: {explicit}")
    loaded = []
    for path in (explicit, Path.cwd() / ".env", user_env_file()):
        if path is not None and path.is_file():
            load_dotenv(path, override=False)
            loaded.append(path)
    return loaded
