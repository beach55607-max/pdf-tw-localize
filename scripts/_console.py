#!/usr/bin/env python3
"""Encoding-safe CLI output helpers for Windows and redirected streams."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO


def _stream_supports(stream: TextIO, text: str) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding, errors="strict")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def emit_json(payload: Any, *, stream: TextIO | None = None, indent: int = 2) -> None:
    """Write valid JSON without raising on strict CP950 Windows consoles.

    Human-readable Unicode is used when the target stream supports it.  If a
    character (for example ©) cannot be represented, the whole JSON document
    is emitted with standard ``\\uXXXX`` escapes so it remains valid JSON and
    round-trips without data loss.
    """

    destination = stream or sys.stdout
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    if not _stream_supports(destination, text):
        text = json.dumps(payload, ensure_ascii=True, indent=indent)
    destination.write(text + "\n")
    destination.flush()


def emit_text(value: Any, *, stream: TextIO | None = None) -> None:
    """Write a log line with a reversible escape fallback for legacy consoles."""

    destination = stream or sys.stdout
    text = str(value)
    if not _stream_supports(destination, text):
        encoding = getattr(destination, "encoding", None) or "ascii"
        try:
            text = text.encode(encoding, errors="backslashreplace").decode(encoding)
        except LookupError:
            text = text.encode("ascii", errors="backslashreplace").decode("ascii")
    destination.write(text + "\n")
    destination.flush()
