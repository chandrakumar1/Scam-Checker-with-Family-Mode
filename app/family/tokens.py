"""Invite tokens and link references.

An invite token is 128 bits of randomness carried in the Telegram deep link
(`/start fam_<token>`). Only its SHA-256 hash is stored, it works once, and it
expires. It must never be logged.
"""

import hashlib
import re
import secrets

INVITE_PREFIX = "fam_"
TOKEN_BYTES = 16  # 128 bits

# token_urlsafe(16) is exactly 22 URL-safe characters.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22}")
_REF_RE = re.compile(r"[0-9a-f]{16}")


def new_invite_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_valid_token(token: object) -> bool:
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


def start_payload(token: str) -> str:
    """The `/start` payload for a deep link (Telegram allows 64 characters)."""
    if not is_valid_token(token):
        raise ValueError("not a valid invite token")
    return f"{INVITE_PREFIX}{token}"


def parse_start_payload(payload: str | None) -> str | None:
    """The token inside a `/start` payload, or None if it is not a well-formed invite."""
    if not payload or not payload.startswith(INVITE_PREFIX):
        return None
    token = payload[len(INVITE_PREFIX):]
    return token if is_valid_token(token) else None


def new_ref() -> str:
    """Random link reference for button data (never a sequential id)."""
    return secrets.token_hex(8)


def is_valid_ref(ref: object) -> bool:
    return isinstance(ref, str) and _REF_RE.fullmatch(ref) is not None
