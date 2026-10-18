"""
Async SQLAlchemy database session management.
Single connection pool shared across all requests in a service.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from shared.common.config import BaseServiceSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lightweight additive migrations.
#
# ``Base.metadata.create_all`` only creates *missing tables* — it never ALTERs a
# table that already exists. When a new column is added to a model whose table
# already shipped, every SELECT then fails with ``UndefinedColumn``. Each entry
# below is an idempotent ``ADD COLUMN IF NOT EXISTS`` for such a column. Every
# service runs these on startup; ``ALTER TABLE IF EXISTS`` means a service that
# doesn't own a table simply skips it instead of erroring.
# ---------------------------------------------------------------------------
_ADDITIVE_COLUMNS: tuple[str, ...] = (
    # ssh-executor: Phase 2 cross-platform node capabilities.
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS os_type VARCHAR(32)",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS gpu_vendor VARCHAR(32)",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS gpu_backend VARCHAR(32)",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS gpu_model VARCHAR(256)",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS gpu_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS gpu_memory_gb DOUBLE PRECISION",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS python_version VARCHAR(32)",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS execution_backend VARCHAR(32) NOT NULL DEFAULT 'ssh'",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS resource_enforcement VARCHAR(16) NOT NULL DEFAULT 'best_effort'",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS capabilities_json TEXT",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS health_status VARCHAR(16) NOT NULL DEFAULT 'unknown'",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS last_health_check TIMESTAMPTZ",
    # ssh-executor: training engine label ("python" or "spark_ray").
    "ALTER TABLE IF EXISTS training_runs ADD COLUMN IF NOT EXISTS engine VARCHAR(16) NOT NULL DEFAULT 'python'",
    # ssh-executor: GPU slice per node and execution policy per batch.
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS allocated_gpu_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS allocated_gpu_memory_gb DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS gpu_runtime VARCHAR(32)",
    "ALTER TABLE IF EXISTS task_batches ADD COLUMN IF NOT EXISTS execution_policy VARCHAR(16) NOT NULL DEFAULT 'safe_background'",
    # ssh-executor: optional Spark + Ray engine, custom and Spark MLlib models.
    "ALTER TABLE IF EXISTS training_runs ADD COLUMN IF NOT EXISTS details_json TEXT",
    "ALTER TABLE IF EXISTS training_runs ADD COLUMN IF NOT EXISTS model_code TEXT",
    "ALTER TABLE IF EXISTS training_runs ADD COLUMN IF NOT EXISTS artifact BYTEA",
    # storage-service: which store holds each dataset file. Rows from before
    # this column existed were all written to HDFS.
    "ALTER TABLE IF EXISTS datasets ADD COLUMN IF NOT EXISTS storage_backend VARCHAR(16) NOT NULL DEFAULT 'hdfs'",
    # ssh-executor: projects (folders with a command, environment and outputs).
    "ALTER TABLE IF EXISTS task_batches ADD COLUMN IF NOT EXISTS project_json TEXT",
    "ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS output_files INTEGER",
    "ALTER TABLE IF EXISTS tasks ADD COLUMN IF NOT EXISTS output_bytes BIGINT",
    "ALTER TABLE IF EXISTS ssh_nodes ADD COLUMN IF NOT EXISTS env_cache_gb DOUBLE PRECISION NOT NULL DEFAULT 0",
)


class Base(DeclarativeBase):
    """Base class for all ORM models. Provides __tablename__ auto-generation."""
    pass


class DatabaseManager:
    """
    Manages the async database engine and session factory.
    Usage:
        db = DatabaseManager(settings)
        await db.initialize()
        async with db.session() as session:
            result = await session.execute(...)
    """

    def __init__(self, settings: BaseServiceSettings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    async def initialize(self) -> None:
        """Create the engine and session factory. Call once at app startup."""
        # Convert sync postgres:// URL to async postgresql+asyncpg://
        db_url = self._settings.database_url.replace(
            "postgresql://", "postgresql+asyncpg://"
        )

        # BUG-14 fix: NullPool is used in test environments to avoid connection leaks, not in normal debug.
        pool_class = NullPool if getattr(self._settings, "use_null_pool", False) else None

        engine_kwargs: dict = {
            "url": db_url,
            "echo": self._settings.database_echo,
        }

        if pool_class is not None:
            engine_kwargs["poolclass"] = pool_class
        else:
            engine_kwargs.update(
                {
                    "pool_size": self._settings.database_pool_size,
                    "max_overflow": self._settings.database_max_overflow,
                    "pool_timeout": self._settings.database_pool_timeout,
                    "pool_pre_ping": True,  # Detect stale connections
                }
            )

        self._engine = create_async_engine(**engine_kwargs)
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
        logger.info("Database engine initialized")

    async def create_tables(self) -> None:
        """Create missing tables, then apply idempotent additive column migrations."""
        assert self._engine is not None, "Call initialize() first"
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for statement in _ADDITIVE_COLUMNS:
                await conn.execute(text(statement))
        logger.info("Database tables created and additive migrations applied")

    async def dispose(self) -> None:
        """Cleanly close all connections. Call on app shutdown."""
        if self._engine:
            await self._engine.dispose()
            logger.info("Database engine disposed")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Provide a transactional database session.
        Commits on success, rolls back on any exception.
        """
        assert self._session_factory is not None, "Call initialize() first"
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
