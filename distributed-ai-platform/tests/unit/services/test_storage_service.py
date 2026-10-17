import io
import sys
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from unittest.mock import AsyncMock, MagicMock

from shared.common.config import get_base_settings
from tests.conftest import import_service_module

# Load the real storage-service modules (services/storage-service/*.py). Import
# ``routes`` exactly once: it pulls in dataset_service and models, and importing
# ``models`` a second time would redefine the ``datasets`` table on the shared
# SQLAlchemy metadata.
routes_mod = import_service_module("storage-service", "routes")
DatasetService = routes_mod.DatasetService
dataset_service_mod = sys.modules[DatasetService.__module__]
Dataset = dataset_service_mod.Dataset


blob_store = sys.modules["blob_store"]


class _FakeUploadFile:
    """Minimal stand-in for FastAPI's UploadFile: a name and a binary file."""

    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self.file = io.BytesIO(content)


class FakeHdfs:
    """An in-memory stand-in for the optional HDFS store."""

    name = "hdfs"

    def __init__(self, files=None, up=True):
        self.files = dict(files or {})
        self.up = up
        self.calls = []

    async def put(self, key, src, max_bytes):
        self.calls.append(("put", key))
        self.files[key] = src.read()
        return len(self.files[key])

    async def open(self, key):
        self.calls.append(("open", key))
        if not self.up or key not in self.files:
            raise blob_store.StorageError("unreachable")
        yield self.files[key]

    async def delete(self, key):
        self.calls.append(("delete", key))
        self.files.pop(key, None)

    async def ready(self):
        return self.up


def _settings(tmp_path, backend="local", max_bytes=10_000):
    return SimpleNamespace(
        storage_backend=backend,
        max_upload_size_bytes=max_bytes,
        hdfs_base_path="/platform",
        data_dir=str(tmp_path),
        min_free_disk_bytes=0,
    )


def _service(session, tmp_path, backend="local", hdfs=None, max_bytes=10_000):
    local = blob_store.LocalFileStore(tmp_path)
    local.prepare()
    stores = {"local": local, "hdfs": hdfs or FakeHdfs()}
    return DatasetService(session, stores, _settings(tmp_path, backend, max_bytes)), stores


async def _read(chunks):
    return b"".join([c async for c in chunks])


@pytest.mark.asyncio
async def test_create_dataset_stores_a_local_file(tmp_path):
    db_mock = AsyncMock()
    # session.add() is synchronous in SQLAlchemy; keep it a plain mock so it
    # does not create an un-awaited coroutine.
    db_mock.add = MagicMock()
    service, _ = _service(db_mock, tmp_path)

    result = await service.create_dataset(
        file=_FakeUploadFile("test.csv", b"header1,header2\n1,2"),
        name="test_dataset",
        description="A test dataset",
        created_by="test_user",
    )

    assert result.name == "test_dataset"
    assert result.file_format == "csv"
    assert result.storage_backend == "local"
    assert result.hdfs_path == f"raw/{result.id}.csv"
    assert result.size_bytes == len(b"header1,header2\n1,2")
    assert result.created_by == "test_user"
    assert (tmp_path / "raw" / f"{result.id}.csv").read_bytes() == b"header1,header2\n1,2"
    db_mock.add.assert_called_once()
    db_mock.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_dataset_can_still_write_to_hdfs(tmp_path):
    db_mock = AsyncMock()
    db_mock.add = MagicMock()
    service, stores = _service(db_mock, tmp_path, backend="hdfs")
    result = await service.create_dataset(_FakeUploadFile("d.parquet", b"PAR1"), "d", None, "u")
    assert result.storage_backend == "hdfs"
    assert result.hdfs_path == f"/platform/raw/{result.id}.parquet"
    assert stores["hdfs"].files[result.hdfs_path] == b"PAR1"
    assert not list((tmp_path / "raw").iterdir())


@pytest.mark.asyncio
async def test_create_dataset_rejects_unsupported_extension(tmp_path):
    from shared.common.exceptions import ValidationError

    service, _ = _service(AsyncMock(), tmp_path)
    with pytest.raises(ValidationError):
        await service.create_dataset(_FakeUploadFile("notes.txt", b"nope"), "bad", None, None)
    assert not list((tmp_path / "raw").iterdir())


@pytest.mark.asyncio
async def test_create_dataset_refuses_files_over_the_limit(tmp_path):
    db_mock = AsyncMock()
    db_mock.add = MagicMock()
    service, _ = _service(db_mock, tmp_path, max_bytes=10)
    with pytest.raises(blob_store.FileTooLarge) as err:
        await service.create_dataset(_FakeUploadFile("big.csv", b"x" * 11), "big", None, "u")
    assert err.value.http_status == 413
    assert not list((tmp_path / "raw").iterdir())  # no partial or temporary file
    db_mock.add.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_commit_removes_the_stored_file(tmp_path):
    db_mock = AsyncMock()
    db_mock.add = MagicMock()
    db_mock.commit.side_effect = RuntimeError("database down")
    service, _ = _service(db_mock, tmp_path)
    with pytest.raises(RuntimeError):
        await service.create_dataset(_FakeUploadFile("a.csv", b"a\n1"), "a", None, "u")
    assert not list((tmp_path / "raw").iterdir())
    db_mock.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# Owner scoping in DatasetService (real queries against in-memory SQLite)
# ---------------------------------------------------------------------------

ALICE_ID = uuid.uuid4()
ALICE_ID_2 = uuid.uuid4()
BOB_ID = uuid.uuid4()


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Dataset.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                # Datasets from before 2.0 live in HDFS ...
                Dataset(id=ALICE_ID, name="a1", storage_backend="hdfs", hdfs_path=f"/platform/raw/{ALICE_ID}.csv",
                        file_format="csv", created_by="alice", validation_status="pending"),
                Dataset(id=ALICE_ID_2, name="a2", storage_backend="hdfs", hdfs_path=f"/platform/raw/{ALICE_ID_2}.csv",
                        file_format="csv", created_by="alice", validation_status="pending"),
                # ... newer ones in local files.
                Dataset(id=BOB_ID, name="b1", storage_backend="local", hdfs_path=f"raw/{BOB_ID}.csv",
                        file_format="csv", created_by="bob", validation_status="pending", size_bytes=7),
            ]
        )
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_dataset_only_returns_owned(session_factory, tmp_path):
    async with session_factory() as session:
        service, _ = _service(session, tmp_path)
        assert (await service.get_dataset(ALICE_ID, "alice")).name == "a1"
        assert await service.get_dataset(ALICE_ID, "bob") is None
        assert await service.get_dataset(uuid.uuid4(), "alice") is None


@pytest.mark.asyncio
async def test_list_datasets_scoped_to_owner(session_factory, tmp_path):
    async with session_factory() as session:
        service, _ = _service(session, tmp_path)
        items, total = await service.list_datasets("alice")
        assert total == 2
        assert {d.name for d in items} == {"a1", "a2"}

        items, total = await service.list_datasets("bob")
        assert total == 1
        assert [d.name for d in items] == ["b1"]

        items, total = await service.list_datasets("mallory")
        assert (items, total) == ([], 0)


@pytest.mark.asyncio
async def test_download_reads_each_dataset_from_its_own_store(session_factory, tmp_path):
    from shared.common.exceptions import ValidationError

    hdfs = FakeHdfs({f"/platform/raw/{ALICE_ID}.csv": b"from,hdfs"})
    async with session_factory() as session:
        service, _ = _service(session, tmp_path, hdfs=hdfs)
        (tmp_path / "raw" / f"{BOB_ID}.csv").write_bytes(b"x,y\n1,2")

        chunks, fmt, size = await service.download_dataset(BOB_ID, "bob")
        assert (await _read(chunks), fmt, size) == (b"x,y\n1,2", "csv", 7)

        chunks, _, _ = await service.download_dataset(ALICE_ID, "alice")
        assert await _read(chunks) == b"from,hdfs"
        assert hdfs.calls == [("open", f"/platform/raw/{ALICE_ID}.csv")]

        hdfs.calls.clear()
        with pytest.raises(ValidationError):
            await service.download_dataset(BOB_ID, "alice")
        with pytest.raises(ValidationError):
            await service.download_dataset(ALICE_ID, "bob")
        assert hdfs.calls == []


@pytest.mark.asyncio
async def test_download_explains_an_hdfs_dataset_when_hdfs_is_off(session_factory, tmp_path):
    async with session_factory() as session:
        service, _ = _service(session, tmp_path, hdfs=FakeHdfs(up=False))
        with pytest.raises(blob_store.StoreUnavailable) as err:
            await service.download_dataset(ALICE_ID, "alice")
        assert err.value.http_status == 503
        assert "make migrate-datasets" in err.value.message


@pytest.mark.asyncio
async def test_download_of_a_missing_local_file_is_an_error_not_a_broken_stream(session_factory, tmp_path):
    async with session_factory() as session:
        service, _ = _service(session, tmp_path)
        with pytest.raises(blob_store.StorageError):
            await service.download_dataset(BOB_ID, "bob")


@pytest.mark.asyncio
async def test_delete_dataset_rejects_other_owner(session_factory, tmp_path):
    hdfs = FakeHdfs()
    async with session_factory() as session:
        service, _ = _service(session, tmp_path, hdfs=hdfs)
        bob_file = tmp_path / "raw" / f"{BOB_ID}.csv"
        bob_file.write_bytes(b"x")
        assert await service.delete_dataset(BOB_ID, "alice") is False
        assert bob_file.exists()

        assert await service.delete_dataset(BOB_ID, "bob") is True
        assert not bob_file.exists()

        # An HDFS dataset is removed from HDFS, even when HDFS fails.
        assert await service.delete_dataset(ALICE_ID, "alice") is True
        assert hdfs.calls == [("delete", f"/platform/raw/{ALICE_ID}.csv")]

    async with session_factory() as session:
        service, _ = _service(session, tmp_path)
        assert await service.get_dataset(BOB_ID, "bob") is None
        # Alice's other data is untouched.
        _, total = await service.list_datasets("alice")
        assert total == 1


# ---------------------------------------------------------------------------
# Owner resolution: jwt.sub, or X-On-Behalf-Of for internal tokens only
# ---------------------------------------------------------------------------

def test_resolve_owner_uses_sub_for_regular_tokens():
    assert routes_mod.resolve_owner_id({"sub": "alice"}, None) == "alice"


def test_resolve_owner_ignores_header_for_non_internal_tokens():
    assert routes_mod.resolve_owner_id({"sub": "alice"}, "bob") == "alice"
    # Only the literal boolean claim counts, not a truthy string.
    assert routes_mod.resolve_owner_id({"sub": "alice", "internal": "true"}, "bob") == "alice"


def test_resolve_owner_honours_header_for_internal_tokens():
    user = {"sub": "ssh-executor", "internal": True}
    assert routes_mod.resolve_owner_id(user, "bob") == "bob"
    # Internal token without the header acts as itself.
    assert routes_mod.resolve_owner_id(user, None) == "ssh-executor"
    assert routes_mod.resolve_owner_id(user, "  ") == "ssh-executor"


def test_resolve_owner_requires_subject():
    with pytest.raises(HTTPException) as exc:
        routes_mod.resolve_owner_id({}, None)
    assert exc.value.status_code == 401


class _RecordingService:
    """Fake DatasetService that records which owner each call was scoped to."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def list_datasets(self, owner_id, skip=0, limit=100):
        self.calls.append(("list", owner_id))
        return [], 0

    async def get_dataset(self, dataset_id, owner_id):
        self.calls.append(("get", owner_id))
        return None

    async def download_dataset(self, dataset_id, owner_id):
        from shared.common.exceptions import ValidationError

        self.calls.append(("download", owner_id))
        raise ValidationError("Dataset not found")

    async def delete_dataset(self, dataset_id, owner_id):
        self.calls.append(("delete", owner_id))
        return False


def _token(sub: str, **claims) -> str:
    settings = get_base_settings()
    now = int(time.time())
    payload = {"sub": sub, "iat": now, "exp": now + 300, **claims}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


@pytest.fixture
def api():
    fake = _RecordingService()
    app = FastAPI()
    app.include_router(routes_mod.router, prefix="/api/v1/storage")
    app.dependency_overrides[routes_mod.get_dataset_service] = lambda: fake
    with TestClient(app) as client:
        yield client, fake


def test_routes_scope_every_read_and_delete_to_caller(api):
    client, fake = api
    headers = {"Authorization": f"Bearer {_token('alice')}"}
    ds = uuid.uuid4()

    assert client.get("/api/v1/storage/datasets", headers=headers).status_code == 200
    assert client.get(f"/api/v1/storage/datasets/{ds}", headers=headers).status_code == 404
    assert client.get(f"/api/v1/storage/datasets/{ds}/download", headers=headers).status_code == 404
    assert client.delete(f"/api/v1/storage/datasets/{ds}", headers=headers).status_code == 404

    assert fake.calls == [
        ("list", "alice"), ("get", "alice"), ("download", "alice"), ("delete", "alice"),
    ]


def test_routes_ignore_on_behalf_of_for_user_tokens(api):
    client, fake = api
    headers = {"Authorization": f"Bearer {_token('alice')}", "X-On-Behalf-Of": "bob"}
    client.get("/api/v1/storage/datasets", headers=headers)
    client.get(f"/api/v1/storage/datasets/{uuid.uuid4()}/download", headers=headers)
    assert fake.calls == [("list", "alice"), ("download", "alice")]


def test_routes_honour_on_behalf_of_for_internal_tokens(api):
    from shared.common.auth import create_internal_token

    client, fake = api
    headers = {
        "Authorization": f"Bearer {create_internal_token('ssh-executor')}",
        "X-On-Behalf-Of": "bob",
    }
    client.get(f"/api/v1/storage/datasets/{uuid.uuid4()}", headers=headers)
    client.get(f"/api/v1/storage/datasets/{uuid.uuid4()}/download", headers=headers)
    assert fake.calls == [("get", "bob"), ("download", "bob")]


def test_production_refuses_the_example_database_password():
    from shared.common.security import assert_secure_database_url

    example = "postgresql://platform:platform_secret@postgres:5432/platform_db"
    assert_secure_database_url("development", example)  # allowed locally
    for env in ("production", "staging"):
        with pytest.raises(RuntimeError) as err:
            assert_secure_database_url(env, example)
        assert "platform_secret" not in str(err.value)  # never echoed
        with pytest.raises(RuntimeError):
            assert_secure_database_url(env, "postgresql://platform@postgres/platform_db")
    assert_secure_database_url("production", "postgresql://platform:Xk2v9QpLr7wE4tYz@postgres/platform_db")
