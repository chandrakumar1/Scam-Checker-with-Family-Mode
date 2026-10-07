"""Database engine and sessions.

`DATABASE_URL` is the only place a database is chosen. Everything
SQLite-specific (pragmas, the immediate-transaction hook, thread settings,
file permissions) lives in `_configure_sqlite` and `_secure_sqlite_file` and is
applied only for `sqlite` URLs, so moving to Postgres later means changing the
URL, adding a driver and swapping `create_all` for migrations.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, event
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.db import models  # noqa: F401  (importing registers the tables on SQLModel.metadata)

SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


def _is_sqlite(database_url: str) -> bool:
    return make_url(database_url).get_backend_name() == "sqlite"


def _sqlite_database_path(database_url: str) -> str | None:
    """The file path of a SQLite URL, or None for an in-memory database."""
    database = make_url(database_url).database
    if not database or database == ":memory:" or "mode=memory" in database:
        return None
    return database


def _configure_sqlite(engine: Engine, *, in_memory: bool) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        # Take over transaction control: the "begin" hook below emits BEGIN
        # IMMEDIATE, so writers are serialised and a transaction that reads
        # then writes can never be overtaken (the SQLAlchemy-documented recipe).
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
        if not in_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _secure_sqlite_file(database_url: str) -> None:
    """The file holds Telegram ids and names: make it private to this user."""
    path = _sqlite_database_path(database_url)
    if path and os.path.exists(path):
        os.chmod(path, 0o600)


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    if not _is_sqlite(database_url):
        return sa_create_engine(database_url, echo=echo)

    in_memory = _sqlite_database_path(database_url) is None
    kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS}}
    if in_memory:
        # One shared connection, so every session sees the same in-memory
        # database. Fine for sequential use; concurrent use needs a file.
        kwargs["poolclass"] = StaticPool
    engine = sa_create_engine(database_url, echo=echo, **kwargs)
    _configure_sqlite(engine, in_memory=in_memory)
    return engine


def init_db(engine: Engine, database_url: str | None = None) -> None:
    """Create any missing tables (Phase 3 has no migrations yet)."""
    SQLModel.metadata.create_all(engine)
    _secure_sqlite_file(database_url or engine.url.render_as_string(hide_password=False))


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any error."""
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
