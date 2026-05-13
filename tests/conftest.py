"""Shared pytest fixtures.

Provides an in-memory SQLite session for tests that need DB-backed
lifecycle / scoring logic. Avoids touching the real disk database
or pulling environment-specific config.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db_session():
    """In-memory SQLite session with all app.models tables created.
    Yields a Session; rolls back + closes on teardown."""
    from app.models import Base
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()
        engine.dispose()
