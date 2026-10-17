"""Move dataset files from HDFS to local storage.

Run inside the storage-service container while HDFS is up (``make
migrate-datasets`` does both):

    python -m migrate_datasets            # move every HDFS dataset
    python -m migrate_datasets --dry-run  # only list what would move

Each file is copied, its size checked, and only then is its row switched to
the local store, one dataset at a time. Re-running skips datasets already
moved. The HDFS copies are left in place; remove the HDFS volumes yourself once
you are satisfied.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

from sqlalchemy import select

from blob_store import HDFS, LOCAL, build_stores, local_key
from config import get_storage_settings
from database import get_db_manager
from models import Dataset


async def migrate_rows(session, local, hdfs, max_bytes: int, dry_run: bool = False, out=sys.stdout) -> tuple[int, int]:
    """Move every HDFS dataset in ``session`` to ``local``. Returns (moved, failed)."""
    # Plain values, read up front: a rollback after a failed row expires every
    # loaded object, and an async session cannot lazily reload them.
    rows = [
        SimpleNamespace(id=d.id, name=d.name, owner=d.created_by, file_format=d.file_format,
                        key=d.hdfs_path, size_bytes=d.size_bytes)
        for d in (
            await session.execute(select(Dataset).where(Dataset.storage_backend == HDFS))
        ).scalars().all()
    ]
    print(f"{len(rows)} dataset(s) stored in HDFS.", file=out)
    if rows and not dry_run and not await hdfs.ready():
        print("HDFS is not reachable. Start it first: make up-hdfs", file=out)
        return 0, len(rows)
    moved = failed = 0
    for row in rows:
        label = f"{row.id} ({row.name}, owner {row.owner})"
        if dry_run:
            print(f"  would move {label}", file=out)
            continue
        new_key = local_key(str(row.id), row.file_format)
        try:
            size = await _copy(hdfs, local, row.key, new_key, max_bytes)
            if row.size_bytes is not None and row.size_bytes != size:
                raise RuntimeError(f"size mismatch: expected {row.size_bytes}, copied {size}")
            ds = await session.get(Dataset, row.id)
            if ds is None or ds.storage_backend != HDFS:
                raise RuntimeError("the dataset changed while migrating")
            ds.storage_backend = LOCAL
            ds.hdfs_path = new_key
            ds.size_bytes = size
            await session.commit()
            moved += 1
            print(f"  moved {label}: {size} bytes", file=out)
        except Exception as exc:  # keep going; this row stays on HDFS
            await session.rollback()
            await local.delete(new_key)
            failed += 1
            print(f"  FAILED {label}: {exc}", file=out)
    print(f"Moved {moved}, failed {failed}.", file=out)
    return moved, failed


async def migrate(dry_run: bool = False, out=sys.stdout) -> tuple[int, int]:
    """Run :func:`migrate_rows` against the configured database."""
    settings = get_storage_settings()
    stores = build_stores(settings)
    stores[LOCAL].prepare()
    db = get_db_manager()
    await db.initialize()
    await db.create_tables()  # also adds the storage_backend column if missing
    try:
        async with db.session() as session:
            return await migrate_rows(
                session, stores[LOCAL], stores[HDFS], settings.max_upload_size_bytes, dry_run, out
            )
    finally:
        await db.dispose()


async def _copy(hdfs, local, hdfs_key: str, new_key: str, max_bytes: int) -> int:
    """Stream the HDFS file to a temporary file, then store it locally."""
    fd, tmp = tempfile.mkstemp(dir=local.root, prefix=".migrate-")
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in hdfs.open(hdfs_key):
                fh.write(chunk)
        with open(tmp, "rb") as src:
            return await local.put(new_key, src, max_bytes)
    finally:
        os.unlink(tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description="Move dataset files from HDFS to local storage.")
    parser.add_argument("--dry-run", action="store_true", help="list what would move; change nothing")
    args = parser.parse_args()
    _moved, failed = asyncio.run(migrate(dry_run=args.dry_run))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
