"""Encryption helpers for secrets stored in the database (Binance API keys)."""
import base64
import hashlib
import sys

from cryptography.fernet import Fernet, InvalidToken

from app.config import Config

_PREFIX = "enc1:"


class EncryptionNotConfigured(RuntimeError):
    pass


def _derive_key(secret: str) -> bytes:
    """Use the secret directly if it is already a valid Fernet key; otherwise derive one from it.
    This lets operators use any long random string (e.g. one Render generates), no tooling required."""
    raw = secret.strip().encode()
    try:
        Fernet(raw)
        return raw
    except Exception:
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def _fernet() -> Fernet:
    if not Config.ENCRYPTION_KEY:
        raise EncryptionNotConfigured("ENCRYPTION_KEY is not set")
    return Fernet(_derive_key(Config.ENCRYPTION_KEY))


def cipher() -> Fernet:
    """Public accessor (used by the backup module to encrypt whole-database backups)."""
    return _fernet()


def encrypt(value: str) -> str:
    """Encrypt a secret. Empty strings stay empty. Already-encrypted values pass through."""
    if not value or value.startswith(_PREFIX):
        return value
    return _PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt a stored secret. Legacy plaintext (no prefix) is returned unchanged."""
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        raise ValueError("Cannot decrypt stored secret: wrong ENCRYPTION_KEY?")


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_PREFIX)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "genkey":
        print(Fernet.generate_key().decode())
    else:
        print("usage: python -m app.security genkey")
