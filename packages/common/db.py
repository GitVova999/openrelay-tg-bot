"""Async SQLAlchemy engine + session factory. One engine per process."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings


@lru_cache(maxsize=1)
def _engine():
    s = settings()
    return create_async_engine(s.postgres_dsn, pool_size=10, max_overflow=20, pool_pre_ping=True)


@lru_cache(maxsize=1)
def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine(), expire_on_commit=False)


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """Use as `async with session() as s: ...`. Commits on clean exit."""
    async with _sessionmaker()() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
