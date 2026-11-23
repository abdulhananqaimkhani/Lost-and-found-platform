"""Shared pytest fixtures.

Database tests run against a real PostGIS database (TEST_DATABASE_URL), migrated from scratch at
the start of the session. Each test runs inside a transaction that is rolled back afterwards,
so tests never see each other's writes. No paid API and no running Ollama are needed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from lf_core.db import make_engine
from lf_core.demo import load_demo_dataset
from lf_core.settings import REPO_ROOT, Settings, get_settings
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def _test_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    try:
        return get_settings().test_database_url
    except Exception:  # noqa: BLE001 - no .env at all
        return None


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    url = _test_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping database tests")
    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("Refusing to run tests: TEST_DATABASE_URL must point at a database ending in _test")
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Test database unreachable ({type(exc).__name__}); is `docker compose up -d` running?")

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    # Round-trip the migrations: this proves downgrade works too.
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    with Session(engine) as session:
        load_demo_dataset(session)
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def connection(test_engine: Engine):
    conn = test_engine.connect()
    outer = conn.begin()
    try:
        yield conn
    finally:
        outer.rollback()
        conn.close()


@pytest.fixture
def sessions(connection) -> sessionmaker[Session]:
    # Code under test may call session.commit(); with this mode that only releases a savepoint.
    return sessionmaker(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)


@pytest.fixture
def session(sessions) -> Iterator[Session]:
    s = sessions()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused@127.0.0.1:1/unused",
        fingerprint_secret="test-fingerprint-secret",
        read_rate_per_minute=10_000,
        suggestion_rate_per_hour=5,
        show_demo_data=True,
        _env_file=None,
    )
