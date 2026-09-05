import json

import pytest

from srv.vault import VaultError, decrypt, encrypt, load_or_create_key, write


def test_credentials_are_encrypted_authenticated_and_not_written_plaintext(tmp_path):
    key_path = tmp_path / "keys" / "credentials.key"
    vault_path = tmp_path / "data" / "credentials.vault"
    key = load_or_create_key(key_path)
    write(vault_path, {"password": "not-for-disk"}, key)
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert vault_path.stat().st_mode & 0o777 == 0o600
    assert b"not-for-disk" not in vault_path.read_bytes()
    assert decrypt(vault_path.read_bytes(), key)["password"] == "not-for-disk"


def test_wrong_key_and_tampering_are_rejected(tmp_path):
    key = load_or_create_key(tmp_path / "key")
    blob = encrypt({"password": "hidden"}, key)
    with pytest.raises(VaultError):
        decrypt(blob, bytes(byte ^ 1 for byte in key))
    envelope = json.loads(blob)
    envelope["ciphertext"] = envelope["ciphertext"][:-2] + "AA"
    with pytest.raises(VaultError):
        decrypt(json.dumps(envelope).encode(), key)
