"""SQLAlchemy engine and transaction lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Database:
    """Own an engine and provide explicit, atomic session scopes."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        parsed_url = make_url(url)
        if parsed_url.drivername.startswith("sqlite") and parsed_url.database not in (
            None,
            "",
            ":memory:",
        ):
            Path(parsed_url.database).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )

        self._engine = create_engine(url, echo=echo, future=True)
        if parsed_url.drivername.startswith("sqlite"):
            event.listen(self._engine, "connect", _enable_sqlite_foreign_keys)
        self._session_factory = sessionmaker(
            bind=self._engine,
            class_=Session,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> Engine:
        return self._engine

    def create_schema(self) -> None:
        Base.metadata.create_all(self._engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Commit on success and roll back the whole unit of work on failure."""

        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self._engine.dispose()
