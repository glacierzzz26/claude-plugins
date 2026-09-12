"""Prompt loading, untrusted-text fencing, and provenance hashing."""

from __future__ import annotations

import hashlib
import re
import secrets

from . import paths


def _filename(name: str) -> str:
    """Accept either 'fixer' or 'fixer.md'."""
    return name if name.endswith((".md", ".json")) else name + ".md"


_LANGUAGE_NAMES = {"en": "English", "zh": "Simplified Chinese (简体中文)"}


def language_name(lang: str) -> str:
    """Human-readable name, for the instruction given to a model."""
    return _LANGUAGE_NAMES.get(lang, "English")


def detect_language(*texts: str) -> str:
    """Guess the language of user-written text: 'zh' or 'en'.

    Used to answer the issue in the language it was written in, so a Chinese
    issue gets a Chinese comment.  Deliberately crude: code blocks and inline
    code are stripped first, because a Chinese issue about a Python bug is
    mostly English tokens, and CJK prose is otherwise unmistakable.
    """
    sample = "\n".join(t for t in texts if t)
    sample = re.sub(r"```.*?```", "", sample, flags=re.DOTALL)
    sample = re.sub(r"`[^`]*`", "", sample)
    han = sum(1 for ch in sample if "一" <= ch <= "鿿")
    latin = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    # Any real amount of Han prose wins: English issues essentially never
    # contain it, while Chinese technical writing is full of Latin identifiers.
    if han >= 10 and han * 2 >= latin:
        return "zh"
    return "en"


def load_localized(name: str, lang: str) -> str:
    """Load ``name`` in ``lang``, falling back to the base (English) file.

    A language-specific file is optional: a translation that does not exist
    degrades to English rather than failing a run at the last step.
    """
    base = _filename(name)
    if lang and lang != "en":
        stem, _, ext = base.rpartition(".")
        candidate = paths.prompt_path(f"{stem}.{lang}.{ext}")
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return paths.prompt_path(base).read_text(encoding="utf-8")


def load(name: str) -> str:
    return paths.prompt_path(_filename(name)).read_text(encoding="utf-8")


def template_hash(name: str) -> str:
    return hashlib.sha256(load(name).encode("utf-8")).hexdigest()[:12]


def prompt_ver(name: str, model: str) -> str:
    """Stable identifier for 'this prompt, on this model'.

    Recorded on every metric so a prompt edit is attributable — without it a
    change in the numbers cannot be explained, which defeats optimization.
    """
    return f"{name}@{model}@{template_hash(name)}"


def fence_untrusted(text: str, label: str) -> str:
    """Wrap untrusted text so it cannot be mistaken for instructions.

    A per-call random nonce means an attacker cannot close the fence ahead of
    time by embedding a matching delimiter in the issue body.
    """
    nonce = secrets.token_hex(4)
    return (
        f"```{label}-UNTRUSTED:{nonce}\n"
        f"{text}\n"
        f"```{label}-UNTRUSTED:{nonce}\n"
    )


def render(name: str, *, lang: str = "en", **values: str) -> str:
    """Substitute ``{key}`` placeholders without str.format's brace pitfalls.

    ``lang`` selects a translated template when one exists; the base file is
    the fallback, so an untranslated string is never a hard failure.
    """
    text = load_localized(name, lang)
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text
