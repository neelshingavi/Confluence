#!/usr/bin/env python3
"""Create a private .env file with fresh application secrets and an initial owner account."""
from pathlib import Path
import base64
import getpass
import os
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

if ENV_FILE.exists():
    print(f"Refusing to overwrite existing {ENV_FILE}. Keep its keys stable for existing QR tickets and sessions.")
    sys.exit(1)

email = input("Initial owner email [owner@confluence.local]: ").strip().casefold() or "owner@confluence.local"
if "@" not in email or email.startswith("@") or email.endswith("@"):
    print("Please enter a valid email address.")
    sys.exit(1)
password = secrets.token_urlsafe(20)
# A Fernet key is 32 random bytes encoded as URL-safe base64.
fernet_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
lines = [
    "# Private deployment secrets. Do not commit or share this file.",
    f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}",
    f"SESSION_SECRET={secrets.token_urlsafe(48)}",
    f"APP_FERNET_KEY={fernet_key}",
    f"QR_TOKEN_PEPPER={secrets.token_urlsafe(48)}",
    f"BOOTSTRAP_ADMIN_EMAIL={email}",
    f"BOOTSTRAP_ADMIN_PASSWORD={password}",
    "COOKIE_SECURE=false",
    "APP_PORT=8000",
    "",
]
ENV_FILE.write_text("\n".join(lines), encoding="utf-8")
try:
    os.chmod(ENV_FILE, 0o600)
except OSError:
    pass
print("\nCreated .env with unique application secrets.")
print("Initial owner account (shown once by this script):")
print(f"  Email:    {email}")
print(f"  Password: {password}")
print("\nStore these credentials securely. Sign in after startup and change the password.")
print("Do not change APP_FERNET_KEY or QR_TOKEN_PEPPER after tickets have been issued; doing so can invalidate saved QR credentials.")
