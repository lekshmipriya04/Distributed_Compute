"""
High-level service for handling dataset lifecycle:
1. Store the file (local files by default, or HDFS; see blob_store.py)
2. Extract metadata
3. Validate
4. Persist to PostgreSQL
"""
import csv
import io
import json
import uuid
import zipfile
import structlog
from typing import AsyncIterator, Optional
from fastapi import UploadFile

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from blob_store import HDFS, BlobStore, FileTooLarge, StoreUnavailable, new_key
from models import Dataset
from shared.common.exceptions import StorageError, ValidationError

logger = structlog.get_logger(__name__)


class DatasetService:
    def __init__(self, db_session: AsyncSession, stores: dict[str, BlobStore], settings):
        self._db = db_session
        self._stores = stores
        self._settings = settings
        # New files go to the configured store; existing ones are read from
        # the store recorded on their row.
        self._backend = settings.storage_backend
        self._max_bytes = settings.max_upload_size_bytes

    async def _store_new(self, dataset_id: uuid.UUID, file_format: str, src) -> tuple[str, int]:
        key = new_key(self._backend, str(dataset_id), file_format, self._settings)
        size = await self._stores[self._backend].put(key, src, self._max_bytes)
        return key, size

    async def _save(self, dataset: Dataset) -> Dataset:
        """Commit the row; if that fails, remove the file just stored."""
        try:
            self._db.add(dataset)
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            try:
                await self._stores[dataset.storage_backend].delete(dataset.hdfs_path)
            except StorageError:
                logger.warning("orphan_dataset_file", key=dataset.hdfs_path)
            raise
        await self._db.refresh(dataset)
        return dataset

    async def create_dataset(
        self,
        file: UploadFile,
        name: str,
        description: Optional[str],
        created_by: Optional[str] = None,
    ) -> Dataset:
        """Upload file to HDFS and create a database record."""
        # 1. Validate extension
        filename = file.filename or ""
        if not any(filename.endswith(ext) for ext in [".csv", ".parquet"]):
            raise ValidationError("Only .csv or .parquet files are supported")
        
        file_format = "csv" if filename.endswith(".csv") else "parquet"
        dataset_id = uuid.uuid4()

        # 2. Store the file (streamed in chunks, never fully in memory)
        key, size = await self._store_new(dataset_id, file_format, file.file)

        # 3. Save metadata
        dataset = await self._save(
            Dataset(
                id=dataset_id,
                name=name,
                description=description,
                storage_backend=self._backend,
                hdfs_path=key,
                file_format=file_format,
                size_bytes=size,
                created_by=created_by,
                validation_status="pending",
            )
        )
        logger.info("dataset_created", dataset_id=str(dataset_id), backend=self._backend, size=size)
        return dataset

    async def create_dataset_from_images(
        self,
        zip_bytes: bytes,
        name: str,
        description: Optional[str],
        created_by: Optional[str] = None,
        image_size: int = 8,
        color: str = "grayscale",
    ) -> Dataset:
        """Convert a zip of label-foldered images into a flattened pixel CSV.

        Expected zip layout: ``<label>/<image files>`` (folder name = class).
        Each image is decoded, grayscaled, resized to a square ``image_size``,
        and flattened to columns ``p0 … p{S*S-1}`` plus a numeric ``label``.
        The result is stored as an ordinary CSV, like a CSV upload, so the
        whole training pipeline downstream is unchanged;
        ``schema_json`` records the image shape + columns so the UI can auto-fill.
        """
        from PIL import Image  # imported lazily so the module loads without Pillow

        if len(zip_bytes) > self._max_bytes:
            raise FileTooLarge(f"The file is larger than the {self._max_bytes} byte limit")

        s = max(4, int(image_size))
        exts = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
        samples: list[tuple[str, list[int]]] = []
        labels_seen: set[str] = set()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
                if len(parts) < 2 or not parts[-1].lower().endswith(exts):
                    continue  # need a label folder; skip top-level / non-images
                label_name = parts[-2]
                try:
                    with z.open(info) as fh:
                        im = Image.open(io.BytesIO(fh.read())).convert("L").resize((s, s))
                except Exception:
                    continue  # skip unreadable files rather than failing the batch
                samples.append((label_name, list(im.getdata())))
                labels_seen.add(label_name)
        if not samples:
            raise ValidationError(
                "No images found. Expected a .zip laid out as <label>/<image files>."
            )
        # Map label folders -> numeric indices; integer-named folders keep order.
        def _sort_key(v: str):
            try:
                return (0, int(v))
            except ValueError:
                return (1, v)

        class_names = sorted(labels_seen, key=_sort_key)
        label_index = {ln: i for i, ln in enumerate(class_names)}
        px = s * s
        headers = [f"p{i}" for i in range(px)] + ["label"]

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for label_name, pixels in samples:
            writer.writerow(list(pixels) + [label_index[label_name]])
        csv_bytes = buf.getvalue().encode("utf-8")

        dataset_id = uuid.uuid4()
        key, size = await self._store_new(dataset_id, "csv", io.BytesIO(csv_bytes))
        logger.info("image_dataset_stored", dataset_id=str(dataset_id), images=len(samples),
                    side=s, classes=len(class_names))

        schema = {
            "columns": headers,
            "feature_columns": headers[:-1],
            "target_column": "label",
            "num_classes": len(class_names),
            "class_names": class_names,
            "image": {"side": s, "channels": 1},
        }
        dataset = await self._save(
            Dataset(
                id=dataset_id,
                name=name,
                description=description,
                storage_backend=self._backend,
                hdfs_path=key,
                file_format="csv",
                created_by=created_by,
                row_count=len(samples),
                column_count=px + 1,
                size_bytes=size,
                schema_json=json.dumps(schema),
                validation_status="pending",
            )
        )
        logger.info("image_dataset_created", dataset_id=str(dataset_id))
        return dataset

    async def get_dataset(self, dataset_id: uuid.UUID, owner_id: str) -> Optional[Dataset]:
        """Return the dataset if it exists **and** belongs to ``owner_id``.

        A dataset owned by someone else is indistinguishable from a missing one
        (both return None), so callers cannot probe for other users' IDs.
        """
        result = await self._db.execute(
            select(Dataset).where(Dataset.id == dataset_id, Dataset.created_by == owner_id)
        )
        return result.scalar_one_or_none()

    async def download_dataset(
        self, dataset_id: uuid.UUID, owner_id: str
    ) -> tuple[AsyncIterator[bytes], str, Optional[int]]:
        """Stream the raw file of one of ``owner_id``'s datasets.

        Returns (chunks, file_format, size). The first chunk is read before
        returning, so a missing file or an unreachable store is reported as an
        error rather than as a broken download. Raises ValidationError if the
        dataset does not exist or is not owned by ``owner_id`` (a 404).
        """
        dataset = await self.get_dataset(dataset_id, owner_id)
        if dataset is None:
            raise ValidationError("Dataset not found")
        store = self._stores[dataset.storage_backend]
        chunks = store.open(dataset.hdfs_path)
        try:
            first = await anext(chunks, b"")
        except StorageError as exc:
            if dataset.storage_backend == HDFS and not await store.ready():
                raise StoreUnavailable(
                    "This dataset is stored in HDFS, which is not running. Start it with "
                    "`make up-hdfs`, or move your datasets with `make migrate-datasets`."
                ) from exc
            raise

        async def stream() -> AsyncIterator[bytes]:
            if first:
                yield first
            async for chunk in chunks:
                yield chunk

        return stream(), dataset.file_format, dataset.size_bytes

    async def delete_dataset(self, dataset_id: uuid.UUID, owner_id: str) -> bool:
        """Remove one of ``owner_id``'s datasets: its file (best-effort) and DB row.

        Returns False if the dataset does not exist or belongs to another
        owner. A file that cannot be removed (store down, already gone) is
        logged but does not block deleting the record, so the list can always
        be cleaned up.
        """
        dataset = await self.get_dataset(dataset_id, owner_id)
        if dataset is None:
            return False
        try:
            await self._stores[dataset.storage_backend].delete(dataset.hdfs_path)
        except Exception as e:  # best-effort; the DB row is the source of truth for the UI
            logger.warning("dataset_file_delete_failed", dataset_id=str(dataset_id), error=str(e))
        await self._db.delete(dataset)
        await self._db.commit()
        logger.info("dataset_deleted", dataset_id=str(dataset_id))
        return True

    async def list_datasets(
        self, owner_id: str, skip: int = 0, limit: int = 100
    ) -> tuple[list[Dataset], int]:
        """List ``owner_id``'s datasets, newest first, with their total count."""
        owned = Dataset.created_by == owner_id
        total = await self._db.scalar(
            select(func.count()).select_from(Dataset).where(owned)
        )
        if total is None:
            total = 0

        result = await self._db.execute(
            select(Dataset).where(owned).order_by(Dataset.created_at.desc()).offset(skip).limit(limit)
        )
        return list(result.scalars().all()), total
