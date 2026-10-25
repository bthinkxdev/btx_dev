"""Encryption for secrets at rest and unguessable customer tokens (Part 15.1)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from django.conf import settings


def _fernet():
    from cryptography.fernet import Fernet
    key = getattr(settings, 'CRM_ENCRYPTION_KEY', '')
    if not key:
        # Development fallback: derived from SECRET_KEY. Production must set CRM_ENCRYPTION_KEY.
        key = base64.urlsafe_b64encode(hashlib.sha256(('crm-enc:' + settings.SECRET_KEY).encode()).digest())
    elif isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def encrypt(plain: str) -> str:
    if not plain:
        return ''
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:
        return ''


def new_nonce() -> str:
    return secrets.token_hex(16)


def token_from_nonce(purpose: str, nonce: str) -> str:
    """128-bit+ customer token. Derived with the server secret, so the database alone cannot rebuild it."""
    digest = hmac.new(settings.SECRET_KEY.encode(), f'{purpose}:{nonce}'.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:24]).decode().rstrip('=')


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_ip(ip: str | None) -> str:
    if not ip:
        return ''
    return hmac.new(settings.SECRET_KEY.encode(), ip.encode(), hashlib.sha256).hexdigest()[:32]


def sha256_file(fileobj) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: fileobj.read(65536), b''):
        h.update(chunk)
    fileobj.seek(0)
    return h.hexdigest()


def sign_value(value: str, max_age_hint: str = '') -> str:
    from django.core.signing import TimestampSigner
    return TimestampSigner(salt='crm-file').sign(value)


def unsign_value(signed: str, max_age: int = 300) -> str | None:
    from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
    try:
        return TimestampSigner(salt='crm-file').unsign(signed, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
