import os
import base64
import secrets
import pytest
from app.security import hash_password, verify_password, encrypt_qr_token, decrypt_qr_token, qr_token_hash


def test_password_hash_roundtrip():
    password = "Some-Long-Unique-Test-Password-2026!"
    stored = hash_password(password)
    assert stored.startswith("scrypt$")
    assert verify_password(password, stored)
    assert not verify_password(password + "x", stored)


def test_short_password_rejected():
    with pytest.raises(ValueError):
        hash_password("tiny")


def test_token_encryption_and_hash(monkeypatch):
    monkeypatch.setenv("APP_FERNET_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
    monkeypatch.setenv("QR_TOKEN_PEPPER", secrets.token_urlsafe(48))
    token = secrets.token_urlsafe(32)
    ciphertext = encrypt_qr_token(token)
    assert ciphertext != token
    assert decrypt_qr_token(ciphertext) == token
    assert qr_token_hash(token) == qr_token_hash(token)
    assert qr_token_hash(token) != qr_token_hash(token + "x")
