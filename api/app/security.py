import hashlib
import hmac
import os
import secrets
from cryptography.fernet import Fernet, InvalidToken


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Passwords must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return "scrypt$" + salt.hex() + "$" + derived.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=64)
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _fernet() -> Fernet:
    """Fernet is only used for ticket generation (local backend). Not required for scanner-only deployments."""
    key = os.getenv("APP_FERNET_KEY", "")
    if not key:
        raise RuntimeError("APP_FERNET_KEY is required for ticket generation; generate it with Fernet.generate_key().decode()")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError("APP_FERNET_KEY is not a valid Fernet key") from exc


def encrypt_qr_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_qr_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Unable to decrypt saved ticket credentials. APP_FERNET_KEY may have changed.") from exc


def qr_token_hash(token: str) -> str:
    pepper = os.getenv("QR_TOKEN_PEPPER", "")
    if len(pepper) < 32:
        raise RuntimeError("QR_TOKEN_PEPPER must be configured with at least 32 characters")
    return hmac.new(pepper.encode(), token.encode(), hashlib.sha256).hexdigest()


def scan_fingerprint(token: str) -> str:
    # Uses a keyed hash so raw QR credentials are never written to logs or idempotency records.
    return qr_token_hash(token)


def validate_security_configuration() -> None:
    """Validates security config required for the scanner-only Vercel deployment.
    APP_FERNET_KEY is NOT required here — it's only needed for local ticket generation.
    """
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret) < 32 or "change-me" in secret.lower():
        raise RuntimeError("Set SESSION_SECRET to a unique random value of at least 32 characters")
    if len(os.getenv("QR_TOKEN_PEPPER", "")) < 32:
        raise RuntimeError("Set QR_TOKEN_PEPPER to a unique random value of at least 32 characters")
