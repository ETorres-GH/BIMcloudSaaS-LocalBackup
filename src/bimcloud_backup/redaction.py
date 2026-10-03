"""Removal of secrets (tokens, session ids, tickets) from text before it is logged or shown."""

from __future__ import annotations

import re

REDACTED = "<REMOVIDO>"

# Query parameters and fields that authenticate a request or point to a temporary file.
_SECRET_NAMES = r"access_token|refresh_token|session[-_]id|ticket|code|file-uri"
# key=value, as in URLs and form bodies.
_SECRET_PARAMS = re.compile(rf"(?i)\b({_SECRET_NAMES})=[^&\s\"'#]+")
# "key": "value" or 'key': 'value', as in JSON and in the repr of a dict.
_SECRET_FIELDS = re.compile(rf"""(?i)(["'])({_SECRET_NAMES})\1(\s*:\s*)(["'])(?:\\.|(?!\4).)*\4""")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def redact(text: str) -> str:
    """Return `text` with every secret parameter, field and bearer token replaced."""
    text = _SECRET_FIELDS.sub(
        lambda m: (
            f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{m.group(4)}{REDACTED}{m.group(4)}"
        ),
        text,
    )
    text = _SECRET_PARAMS.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return _BEARER.sub(f"Bearer {REDACTED}", text)
