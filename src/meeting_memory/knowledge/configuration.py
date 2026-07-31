"""Shared INI configuration loading for the CLI and scheduled runner."""

from __future__ import annotations

import configparser
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .errors import ConfigurationError


def config_path(value: Optional[str]) -> Optional[Path]:
    explicit = value or os.environ.get("MEETING_MEMORY_CONFIG")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ConfigurationError("configuration file does not exist: %s" % path)
        return path

    candidates = [
        Path.cwd() / "meeting-memory.ini",
        Path(__file__).resolve().parents[3] / "meeting-memory.ini",
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def _parser(value: Optional[str]) -> Tuple[Optional[Path], configparser.ConfigParser]:
    path = config_path(value)
    parser = configparser.ConfigParser(interpolation=None)
    if path is None:
        return None, parser
    try:
        parser.read_string(path.read_text(encoding="utf-8"), source=str(path))
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise ConfigurationError("cannot read configuration file %s: %s" % (path, exc)) from exc
    return path, parser


def path_configuration(value: Optional[str]) -> Dict[str, str]:
    path, parser = _parser(value)
    if path is None:
        return {}
    if not parser.has_section("paths"):
        raise ConfigurationError("configuration file %s is missing [paths]" % path)

    configured: Dict[str, str] = {}
    for key in ("meetings_dir", "output_dir"):
        raw = parser.get("paths", key, fallback="").strip()
        if not raw:
            continue
        configured_path = Path(raw).expanduser()
        if not configured_path.is_absolute():
            configured_path = path.parent / configured_path
        configured[key] = str(configured_path.resolve())
    return configured


def required_storage_paths(value: Optional[str]) -> Tuple[Path, Path]:
    path = config_path(value)
    if path is None:
        raise ConfigurationError("meeting-memory.ini was not found")
    configured = path_configuration(str(path))
    missing = [key for key in ("meetings_dir", "output_dir") if not configured.get(key)]
    if missing:
        raise ConfigurationError(
            "configuration file %s has empty or missing [paths] value(s): %s"
            % (path, ", ".join(missing))
        )
    return Path(configured["meetings_dir"]), Path(configured["output_dir"])


def openrouter_configuration(value: Optional[str]) -> Dict[str, str]:
    _path, parser = _parser(value)
    if not parser.has_section("openrouter"):
        return {}
    return {
        key: parser.get("openrouter", key, fallback="").strip()
        for key in (
            "api_key",
            "model",
            "ask_model",
            "review_model",
            "review_critic_model",
        )
        if parser.get("openrouter", key, fallback="").strip()
    }


def slack_configuration(value: Optional[str]) -> Dict[str, Any]:
    """Load Slack source settings from the shared INI file.

    Channel and excluded-user IDs may be comma-, whitespace-, or
    newline-separated. Keeping this parsing here gives the interactive CLI and
    scheduled runner one definition of the configured Slack source set.
    """
    path, parser = _parser(value)
    if not parser.has_section("slack"):
        return {"channel_ids": [], "excluded_user_ids": []}

    raw_channels = parser.get("slack", "channel_ids", fallback="")
    channel_ids = []
    for channel_id in re.split(r"[\s,]+", raw_channels.strip()):
        if not channel_id or channel_id in channel_ids:
            continue
        if not re.fullmatch(r"[CDG][A-Z0-9]+", channel_id):
            raise ConfigurationError(
                "configuration file %s has invalid Slack channel ID: %s"
                % (path, channel_id)
            )
        channel_ids.append(channel_id)

    raw_excluded_users = parser.get("slack", "excluded_user_ids", fallback="")
    excluded_user_ids = []
    for user_id in re.split(r"[\s,]+", raw_excluded_users.strip()):
        if not user_id or user_id in excluded_user_ids:
            continue
        if not re.fullmatch(r"[UW][A-Z0-9]+", user_id):
            raise ConfigurationError(
                "configuration file %s has invalid excluded Slack user ID: %s"
                % (path, user_id)
            )
        excluded_user_ids.append(user_id)

    return {
        "channel_ids": channel_ids,
        "excluded_user_ids": excluded_user_ids,
        "bot_token": parser.get("slack", "bot_token", fallback="").strip(),
    }


def main() -> int:
    """Print required runner paths as NUL-delimited records for safe shell use."""
    if len(sys.argv) != 2:
        print("usage: python -m meeting_memory.knowledge.configuration CONFIG", file=sys.stderr)
        return 2
    try:
        meetings_dir, output_dir = required_storage_paths(sys.argv[1])
    except ConfigurationError as exc:
        print("ERROR: invalid configuration: %s" % exc, file=sys.stderr)
        return 78
    for path in (meetings_dir, output_dir):
        sys.stdout.buffer.write(os.fsencode(path) + b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
