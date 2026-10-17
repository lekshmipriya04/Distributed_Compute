"""ORM model for dataset metadata persisted in PostgreSQL."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text, Float, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.common.database import Base


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Where the file is: the store ("local" or "hdfs") and its key there
    # (local: "raw/<id>.<ext>"; HDFS: "/platform/raw/<id>.<ext>"). The column
    # keeps its historical name.
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    hdfs_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    file_format: Mapped[str] = mapped_column(String(32), nullable=False)  # csv/parquet/json

    # Dataset statistics (populated after validation)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    column_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    schema_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Quality metrics
    null_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    validation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    validation_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
