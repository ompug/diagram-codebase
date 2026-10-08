"""Database engine and session factory."""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "postgresql://app:FAKE_PASSWORD@db:5432/todos"

engine = create_engine("postgresql://app:FAKE_PASSWORD@db:5432/todos")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
