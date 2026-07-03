"""Rendering of OpenObserve log hits for the terminal.

``render_hit`` returns a plain string (one line per hit); ``write_hit`` adds
rich color for the level when writing to a console. ``--json`` emits one compact
JSON object per line (jq-able).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.markup import escape

__all__ = ["format_timestamp", "render_hit", "write_hit"]

_MESSAGE_KEYS = ("message", "log", "body", "msg")
_LEVEL_KEYS = ("level", "log_level", "severity", "severity_text")

_LEVEL_COLORS = {
    "TRACE": "dim",
    "DEBUG": "cyan",
    "INFO": "green",
    "WARN": "yellow",
    "WARNING": "yellow",
    "ERROR": "red",
    "ERR": "red",
    "FATAL": "bold red",
    "CRITICAL": "bold red",
}


def format_timestamp(micros: int) -> str:
    """Microseconds-since-epoch -> ISO-8601 UTC; ``-`` when zero/missing."""
    if not micros:
        return "-"
    return datetime.fromtimestamp(micros / 1_000_000, tz=UTC).isoformat()


def _first(hit: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = hit.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _message(hit: dict[str, Any]) -> str:
    msg = _first(hit, _MESSAGE_KEYS)
    if msg is not None:
        return msg
    rest = {k: v for k, v in hit.items() if k != "_timestamp"}
    return json.dumps(rest, default=str)


def render_hit(hit: dict[str, Any], *, as_json: bool = False) -> str:
    """Render a single hit to a one-line string."""
    if as_json:
        return json.dumps(hit, default=str, separators=(",", ":"))
    parts = [format_timestamp(int(hit.get("_timestamp") or 0))]
    level = _first(hit, _LEVEL_KEYS)
    if level:
        parts.append(level.upper())
    parts.append(_message(hit))
    return " ".join(parts)


def write_hit(console: Console, hit: dict[str, Any], *, as_json: bool = False) -> None:
    """Print a hit to ``console`` (level-colored in pretty mode)."""
    if as_json:
        # Bypass Console's rendering pipeline entirely (matches common.print_json's plain
        # print()) so --json output can never be wrapped/highlighted, regardless of console
        # width or future rich behavior — not just suppressed via soft_wrap.
        print(render_hit(hit, as_json=True), file=console.file)
        return
    ts = format_timestamp(int(hit.get("_timestamp") or 0))
    level = _first(hit, _LEVEL_KEYS)
    body = escape(_message(hit))  # log bodies may contain [..] rich-markup syntax
    if level:
        color = _LEVEL_COLORS.get(level.upper(), "white")
        console.print(
            f"[dim]{ts}[/dim] [{color}]{level.upper():<5}[/{color}] {body}",
            highlight=False,
        )
    else:
        console.print(f"[dim]{ts}[/dim] {body}", highlight=False)
