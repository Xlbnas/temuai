"""Secret masking utilities."""
from __future__ import annotations

import os
import re
from typing import Final

SENSITIVE_KEYS: Final[tuple[str, ...]] = (
    "APIYI_API_KEY",
    "APP_PASSWORD_HASH",
    "SESSION_SECRET",
    "PASSWORD",
    "SECRET_KEY",
    "PRIVATE_KEY",
    "TOKEN",
)


def mask_string(value: str | None, visible: int = 4) -> str:
    """Mask a secret, leaving only last N chars visible."""
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


def mask_env_value(key: str, value: str | None) -> str:
    """Mask environment variable value if key looks sensitive."""
    if value is None:
        return ""
    upper = key.upper()
    if any(s in upper for s in SENSITIVE_KEYS):
        return mask_string(value)
    return value


def mask_message(message: str) -> str:
    """Mask secrets appearing in arbitrary text (logs, exceptions, JSON)."""
    if not message:
        return message
    # Mask common patterns: "key": "..." , key=... , Bearer ..., etc.
    patterns = [
        (r'([a-zA-Z0-9_]*API[_-]?KEY\s*[:=]\s*)["\']?([\w\-\.]{8,})["\']?', 2),
        (r'([a-zA-Z0-9_]*PASSWORD[_-]?HASH\s*[:=]\s*)["\']?([^"\'\s]+)', 2),
        (r'([a-zA-Z0-9_]*SESSION[_-]?SECRET\s*[:=]\s*)["\']?([^"\'\s]+)', 2),
        (r'(authorization\s*[:=]\s*[Bb]earer\s+)([\w\-\.]+)', 2),
        (r'(api[_-]?key\s*[:=]\s*)["\']?([\w\-\.]{8,})["\']?', 2),
    ]
    for pattern, group in patterns:
        message = re.sub(
            pattern,
            lambda m, g=group: m.group(1) + mask_string(m.group(g)),
            message,
            flags=re.IGNORECASE,
        )
    return message


def load_env_file(path: str | None = None) -> None:
    """Load .env file if python-dotenv is available."""
    from dotenv import load_dotenv

    if path:
        load_dotenv(path)
    else:
        load_dotenv()
