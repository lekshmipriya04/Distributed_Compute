"""Pydantic schemas for the Dataset API."""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DatasetResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    storage_backend: str = "hdfs"
    # Location within that store. ``hdfs_path`` is the historical name of the
    # same value, kept for clients written before 1.0.1.
    storage_path: str = ""
    hdfs_path: str
    file_format: str
    
    row_count: Optional[int] = None
    column_count: Optional[int] = None
    size_bytes: Optional[int] = None
    schema_json: Optional[str] = None
    
    validation_status: str
    validation_message: Optional[str] = None
    
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def _storage_path(self) -> "DatasetResponse":
        if not self.storage_path:
            self.storage_path = self.hdfs_path
        return self


class DatasetListResponse(BaseModel):
    items: list[DatasetResponse]
    total: int


class DatasetCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = None
