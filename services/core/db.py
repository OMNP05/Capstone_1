"""Engine and session factory.

Sync SQLAlchemy on purpose: FastAPI runs `def` endpoints in a threadpool, the
orchestrator runs in a background task, and the seed script is a plain CLI.
One session factory serves all three.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from services.core.config import settings

_connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

engine = create_engine(
    settings.resolved_database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For background tasks and scripts: commit on success, roll back on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def create_all() -> None:
    from services.core import models  # noqa: F401  (register mappers)

    models.Base.metadata.create_all(bind=engine)


def drop_all() -> None:
    from services.core import models  # noqa: F401

    models.Base.metadata.drop_all(bind=engine)
