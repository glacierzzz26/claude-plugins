"""Structured logging to stderr, with secret redaction.

Every log line and every string that might reach a terminal, a comment, or a
metrics file goes through :func:`redact`.  Secrets are registered by
``isolation`` as they are resolved, so nothing needs to remember to scrub them.
"""

from __future__ import annotations

import sys
import re

_SECRETS: set[str] = set()
_VERBOSE = False

# A secret shorter than this is not worth redacting: it would cause false
# positives by matching ordinary text.
_MIN_SECRET_LEN = 8

_VERBOSE = False


def set_verbose(value: bool) -> None:
    global _VERBOSE
    _VERBOSE = bool(value)


def register_secret(value: str | None) -> None:
    """Remember a secret so it is redacted from all future output."""
    if value and len(value) >= _MIN_SECRET_LEN:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "<redacted>")
    return text


def log(message: str, *, level: str = "info") -> None:
    if level == "debug" and not _VERBOSE:
        return
    prefix = {"debug": "·", "info": "→", "warn": "!", "error": "✗"}.get(level, "·")
    print(f"[auto-fix] {prefix} {redact(message)}", file=sys.stderr, flush=True)


def debug(message: str) -> None:
    log(message, level="debug")


def warn(message: str) -> None:
    log(message, level="warn")


def error(message: str) -> None:
    log(message, level="error")
