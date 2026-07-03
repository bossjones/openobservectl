"""Tests for openobservectl.render — timestamp formatting and hit rendering (TDD)."""

from __future__ import annotations

import io
import json

from rich.console import Console

from openobservectl.render import format_timestamp, render_hit, write_hit


def test_format_timestamp_micros_to_iso_utc():
    # 1970-01-01T00:00:01+00:00 == 1_000_000 microseconds
    assert format_timestamp(1_000_000).startswith("1970-01-01T00:00:01")


def test_format_timestamp_zero_or_missing_is_dash():
    assert format_timestamp(0) == "-"


def test_render_hit_json_is_single_line_and_roundtrips():
    hit = {"_timestamp": 1_000_000, "level": "INFO", "log": "hello"}
    out = render_hit(hit, as_json=True)
    assert "\n" not in out
    assert json.loads(out) == hit


def test_render_hit_pretty_contains_ts_level_and_message():
    hit = {"_timestamp": 1_000_000, "level": "warn", "message": "disk almost full"}
    out = render_hit(hit, as_json=False)
    assert "1970-01-01T00:00:01" in out
    assert "WARN" in out  # level upper-cased
    assert "disk almost full" in out


def test_render_hit_prefers_message_then_log_then_body():
    assert "from-log" in render_hit({"_timestamp": 1, "log": "from-log"}, as_json=False)
    assert "from-body" in render_hit({"_timestamp": 1, "body": "from-body"}, as_json=False)


def test_render_hit_without_known_message_dumps_remaining_fields():
    hit = {"_timestamp": 1_000_000, "custom": "x", "n": 3}
    out = render_hit(hit, as_json=False)
    assert "custom" in out and "x" in out
    assert "n" in out


def test_render_hit_tolerates_missing_timestamp():
    out = render_hit({"log": "no ts here"}, as_json=False)
    assert out.startswith("-")
    assert "no ts here" in out


def test_write_hit_json_mode_never_wraps_long_lines():
    # A narrow console would normally soft-wrap long text across multiple lines;
    # --json output must stay one-JSON-object-per-line regardless of console width.
    buf = io.StringIO()
    console = Console(file=buf, width=20, force_terminal=False)
    hit = {"_timestamp": 1_000_000, "level": "INFO", "message": "a" * 100}
    write_hit(console, hit, as_json=True)
    lines = [line for line in buf.getvalue().splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0]) == hit
