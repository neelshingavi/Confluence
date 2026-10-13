import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

_env_path = Path(__file__).resolve().parents[2] / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

_raw_url = os.getenv("DATABASE_URL", "sqlite:///./confluence-dev.db")
if _raw_url.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + _raw_url[len("postgres://"):]
elif _raw_url.startswith("postgresql://") and not _raw_url.startswith("postgresql+psycopg://"):
    DATABASE_URL = "postgresql+psycopg://" + _raw_url[len("postgresql://"):]
else:
    DATABASE_URL = _raw_url

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True, connect_args=connect_args)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
