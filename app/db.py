from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def single_process():
    """Fail fast if a second bot is already using this PostgreSQL database."""
    if engine.dialect.name != "postgresql":
        yield  # isolated SQLite test databases
        return
    async with engine.connect() as connection:
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(7265910041)"))
        await connection.commit()
        if not acquired:
            raise RuntimeError("another_bot_is_running_on_this_database")
        try:
            yield
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(7265910041)"))
            await connection.commit()


async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from . import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
