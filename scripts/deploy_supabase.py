#!/usr/bin/env python3
"""Deploy schema and bootstrap staff user directly to Supabase PostgreSQL."""
import os
import sys
from pathlib import Path
import getpass
import secrets

ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = ROOT / "supabase_schema.sql"

# Add backend to path for security helpers
sys.path.insert(0, str(ROOT / "backend"))
from app.security import hash_password

def main():
    print("=" * 60)
    print("Confluence -> Supabase Database Migration Tool")
    print("=" * 60)

    url = os.getenv("SUPABASE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        print("\nEnter your Supabase connection string.")
        print("Example: postgresql://postgres.xxxx:your-password@aws-0-ap-south-1.pooler.supabase.com:6543/postgres")
        url = input("Connection string: ").strip()

    if not url:
        print("Error: No connection string provided.")
        sys.exit(1)

    # Normalize url for psycopg
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and not url.startswith("postgresql+psycopg://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    print("\nConnecting to Supabase PostgreSQL...")
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            print("Connected successfully!")
            print("Applying schema from supabase_schema.sql...")
            sql_content = SQL_FILE.read_text(encoding="utf-8")
            # Execute statements
            conn.execute(text(sql_content))
            conn.commit()
            print("Schema applied successfully!")

            # Check if staff user exists
            res = conn.execute(text("SELECT COUNT(*) FROM staff_users;")).scalar()
            if res == 0:
                print("\nNo staff user found. Creating initial staff account...")
                email = input("Staff email [scanner@confluence.local]: ").strip().lower() or "scanner@confluence.local"
                password = secrets.token_urlsafe(16)
                pw_hash = hash_password(password)
                user_id = str(secrets.token_hex(16))
                # Add OWNER so they have all permissions (or CHECKIN_STAFF for scanner only)
                conn.execute(
                    text("INSERT INTO staff_users (id, email, password_hash, role, active) VALUES (:id, :email, :pw, 'OWNER', true)"),
                    {"id": user_id, "email": email, "pw": pw_hash}
                )
                conn.commit()
                print(f"\nCreated staff account:")
                print(f"  Email:    {email}")
                print(f"  Password: {password}")
            else:
                print(f"\nDatabase already has {res} staff account(s).")

            print("\n" + "=" * 60)
            print("Supabase database is ready for Confluence!")
            print("=" * 60)
    except Exception as e:
        print(f"\nError connecting/migrating Supabase database: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
