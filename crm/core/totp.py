"""RFC 6238 TOTP (6 digits, 30 s) for optional two-factor sign-in (Part 3.6, 15.1)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + '=' * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f'{value % 1_000_000:06d}'


def verify(secret: str, code: str, window: int = 1, at: float | None = None) -> bool:
    if not secret or not code or not code.isdigit() or len(code) != 6:
        return False
    counter = int((at or time.time()) // 30)
    return any(hmac.compare_digest(_code(secret, counter + d), code) for d in range(-window, window + 1))


def current(secret: str) -> str:
    return _code(secret, int(time.time() // 30))


def provisioning_uri(secret: str, account: str, issuer: str = 'BThinkX Business OS') -> str:
    return f'otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30'
