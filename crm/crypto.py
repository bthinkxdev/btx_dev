"""Fernet field encryption for credential vault (at-rest only)."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet_key_bytes() -> bytes:
    raw = getattr(settings, 'CRM_CREDENTIALS_FERNET_KEY', '') or ''
    raw = raw.strip()
    if raw:
        return raw.encode('utf-8')
    digest = hashlib.sha256(
        (settings.SECRET_KEY + 'crm-credential-vault').encode('utf-8')
    ).digest()
    return base64.urlsafe_b64encode(digest)


def get_fernet() -> Fernet:
    return Fernet(_fernet_key_bytes())


def encrypt_plaintext(value: str) -> str:
    if value is None or value == '':
        return ''
    return get_fernet().encrypt(value.encode('utf-8')).decode('utf-8')


def decrypt_ciphertext(blob: str) -> str:
    if not blob:
        return ''
    try:
        return get_fernet().decrypt(blob.encode('utf-8')).decode('utf-8')
    except InvalidToken:
        return ''
