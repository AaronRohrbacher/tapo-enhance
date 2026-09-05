"""Authenticated encryption for persisted camera credentials."""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD = b"tapo-enhance-credentials-v1"


class VaultError(ValueError):
    """The credential store could not be authenticated or decoded."""


def load_or_create_key(path: Path) -> bytes:
    """Return this installation's key, creating it mode 0600 if absent."""
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = AESGCM.generate_key(bit_length=256)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    if len(key) != 32:
        raise VaultError("invalid installation key")
    return key


def encrypt(payload: dict, key: bytes) -> bytes:
    nonce = os.urandom(12)
    cleartext = json.dumps(payload, separators=(",", ":")).encode()
    envelope = {
        "version": 1,
        "cipher": "aes-256-gcm",
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, cleartext, AAD)).decode(),
    }
    return (json.dumps(envelope, separators=(",", ":")) + "\n").encode()


def decrypt(blob: bytes, key: bytes) -> dict:
    try:
        envelope = json.loads(blob)
        if envelope.get("version") != 1 or envelope.get("cipher") != "aes-256-gcm":
            raise VaultError("unsupported credential vault")
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        payload = json.loads(AESGCM(key).decrypt(nonce, ciphertext, AAD))
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except VaultError:
        raise
    except Exception as exc:
        raise VaultError("credential vault authentication failed") from exc


def write(path: Path, payload: dict, key: bytes) -> None:
    """Atomically replace the vault without writing plaintext to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encrypt(payload, key))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        temporary.unlink(missing_ok=True)
