"""
Encrypted model fields for mailbox credentials (app passwords, OAuth tokens).

Values are encrypted with Fernet (AES-128-CBC + HMAC) before they are written to
the database and decrypted transparently when read.

Key source (in order):
  1. settings.EMAIL_RECEPTIONIST_ENCRYPTION_KEY  (a Fernet key, recommended)
  2. derived from settings.SECRET_KEY            (fallback so dev works out of the box)

Generate a dedicated key once and put it in .env:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

NOTE: if you rely on the SECRET_KEY fallback and later rotate SECRET_KEY, saved
mailbox credentials can no longer be decrypted and users must reconnect.
"""
import base64
import hashlib
import logging

from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

PREFIX = 'enc::'
_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        key = getattr(settings, 'EMAIL_RECEPTIONIST_ENCRYPTION_KEY', '') or ''
        if not key:
            digest = hashlib.sha256(('email-receptionist:' + settings.SECRET_KEY).encode()).digest()
            key = base64.urlsafe_b64encode(digest)
        _fernet = Fernet(key if isinstance(key, bytes) else key.encode())
    return _fernet


def encrypt_value(value):
    if value is None or value == '':
        return value
    if isinstance(value, str) and value.startswith(PREFIX):
        return value  # already encrypted
    token = _get_fernet().encrypt(str(value).encode()).decode()
    return PREFIX + token


def decrypt_value(value):
    if value is None or value == '':
        return value
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value  # legacy plaintext row - returned as-is, encrypted on next save
    try:
        return _get_fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except Exception:
        logger.error('EmailReceptionist: could not decrypt a stored credential (encryption key changed?).')
        return None


class EncryptedTextField(models.TextField):
    """TextField whose contents are encrypted at rest."""

    def from_db_value(self, value, expression, connection):
        return decrypt_value(value)

    def to_python(self, value):
        return decrypt_value(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt_value(value)
