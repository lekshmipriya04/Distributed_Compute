"""Dataset file stores (local files, optional HDFS), the HDFS-to-local
migration, and streamed downloads through the real routes."""
import io
import os
import sys
import uuid
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.common.config import get_base_settings
from tests.conftest import import_service_module

routes_mod = import_service_module("storage-service", "routes")
blob_store = sys.modules["blob_store"]
DatasetService = routes_mod.DatasetService
Dataset = sys.modules["models"].Dataset
migrate_mod = import_service_module("storage-service", "migrate_datasets")
MDataset = migrate_mod.Dataset
mblob = blob_store

KEY = f"raw/{uuid.uuid4()}.csv"


def _local(tmp_path, **kw):
    store = blob_store.LocalFileStore(tmp_path, **kw)
    store.prepare()
    return store


async def _read(it):
    return b"".join([c async for c in it])


# --- LocalFileStore ----------------------------------------------------------

@pytest.mark.asyncio
async def test_local_round_trip(tmp_path):
    store = _local(tmp_path)
    assert await store.put(KEY, io.BytesIO(b"a,b\n1,2\n"), 100) == 8
    assert await _read(store.open(KEY)) == b"a,b\n1,2\n"
    await store.delete(KEY)
    assert not (tmp_path / KEY).exists()
    await store.delete(KEY)  # deleting again is fine


@pytest.mark.asyncio
async def test_local_streams_large_files_in_chunks(tmp_path):
    store = _local(tmp_path)
    data = os.urandom(3 * blob_store.CHUNK + 123)
    await store.put(KEY, io.BytesIO(data), len(data))
    chunks = [c async for c in store.open(KEY)]
    assert len(chunks) == 4 and max(len(c) for c in chunks) == blob_store.CHUNK
    assert b"".join(chunks) == data


@pytest.mark.asyncio
async def test_local_limit_and_failures_leave_no_file(tmp_path):
    store = _local(tmp_path)
    with pytest.raises(blob_store.FileTooLarge):
        await store.put(KEY, io.BytesIO(b"x" * 11), 10)

    class Broken(io.RawIOBase):
        def __init__(self):
            self.n = 0

        def read(self, size=-1):
            self.n += 1
            if self.n > 2:
                raise OSError("disconnected")
            return b"x" * 10

    with pytest.raises(OSError):
        await store.put(KEY, Broken(), 10_000)
    assert sorted(p.name for p in (tmp_path / "raw").iterdir()) == []


@pytest.mark.asyncio
async def test_local_replaces_atomically(tmp_path):
    store = _local(tmp_path)
    await store.put(KEY, io.BytesIO(b"old"), 100)
    with pytest.raises(blob_store.FileTooLarge):
        await store.put(KEY, io.BytesIO(b"new but too long"), 5)
    assert await _read(store.open(KEY)) == b"old"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd",
        "/etc/passwd",
        "raw/../../escape.csv",
        "raw/not-a-uuid.csv",
        f"raw/{uuid.uuid4()}.exe",
        f"raw/{uuid.uuid4()}.csv/../../x.csv",
        f"other/{uuid.uuid4()}.csv",
        "",
    ],
)
async def test_local_refuses_keys_outside_its_layout(tmp_path, key):
    store = _local(tmp_path)
    with pytest.raises(blob_store.StorageError):
        await store.put(key, io.BytesIO(b"x"), 10)
    with pytest.raises(blob_store.StorageError):
        await store.delete(key)


@pytest.mark.asyncio
async def test_local_refuses_a_symlinked_raw_dir_pointing_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "raw").symlink_to(outside)
    store = blob_store.LocalFileStore(root)
    with pytest.raises(blob_store.StorageError):
        await store.put(KEY, io.BytesIO(b"x"), 10)
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_local_missing_file_and_full_disk(tmp_path):
    store = _local(tmp_path)
    with pytest.raises(blob_store.StorageError):
        await _read(store.open(KEY))
    full = _local(tmp_path, min_free_bytes=10**18)
    with pytest.raises(blob_store.StorageFull) as err:
        await full.put(KEY, io.BytesIO(b"x"), 10)
    assert err.value.http_status == 507
    assert await store.ready()


# --- WebHdfsStore (against a simulated NameNode/DataNode) --------------------

class FakeWebHdfs:
    """NameNode answers 307 to a DataNode URL; the DataNode stores the bytes."""

    def __init__(self):
        self.files = {}
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path.removeprefix("/webhdfs/v1")
        op = url.params.get("op")
        self.requests.append((request.method, url.host, op, path))
        if url.host == "namenode" and op in ("CREATE", "OPEN"):
            return httpx.Response(307, headers={"location": f"http://datanode-1:9864/webhdfs/v1{path}?op={op}"})
        if url.host == "datanode-1" and op == "CREATE":
            self.files[path] = request.read()
            return httpx.Response(201)
        if url.host == "datanode-1" and op == "OPEN":
            if path not in self.files:
                return httpx.Response(404)
            return httpx.Response(200, content=self.files[path])
        if op == "DELETE":
            self.files.pop(path, None)
            return httpx.Response(200, json={"boolean": True})
        if op == "GETFILESTATUS":
            return httpx.Response(200, json={})
        return httpx.Response(400)


def _hdfs(fake):
    return blob_store.WebHdfsStore("http://namenode:9870", transport=httpx.MockTransport(fake.handler))


@pytest.mark.asyncio
async def test_hdfs_round_trip_follows_the_namenode_redirect():
    fake = FakeWebHdfs()
    store = _hdfs(fake)
    key = f"/platform/raw/{uuid.uuid4()}.csv"
    assert await store.put(key, io.BytesIO(b"a,b\n1,2\n"), 100) == 8
    assert fake.files[key] == b"a,b\n1,2\n"
    assert await _read(store.open(key)) == b"a,b\n1,2\n"
    await store.delete(key)
    assert key not in fake.files
    assert await store.ready()
    assert [r[1] for r in fake.requests[:2]] == ["namenode", "datanode-1"]


@pytest.mark.asyncio
async def test_hdfs_limit_missing_file_and_bad_keys():
    fake = FakeWebHdfs()
    store = _hdfs(fake)
    key = f"/platform/raw/{uuid.uuid4()}.csv"
    with pytest.raises(blob_store.FileTooLarge):
        await store.put(key, io.BytesIO(b"x" * 11), 10)
    assert key not in fake.files  # the partial file is removed
    with pytest.raises(blob_store.StorageError):
        await _read(store.open(key))
    for bad in ("/platform/../etc/passwd", "relative/path", "/a b"):
        with pytest.raises(blob_store.StorageError):
            await store.delete(bad)


@pytest.mark.asyncio
async def test_hdfs_not_ready_when_unreachable():
    def down(request):
        raise httpx.ConnectError("refused")

    store = blob_store.WebHdfsStore("http://namenode:9870", transport=httpx.MockTransport(down))
    assert await store.ready() is False


# --- migration from HDFS to local files ---------------------------------------

class MemHdfs:
    name = "hdfs"

    def __init__(self, files, up=True):
        self.files = files
        self.up = up

    async def open(self, key):
        if key not in self.files:
            raise mblob.StorageError("missing")
        yield self.files[key]

    async def delete(self, key):
        self.files.pop(key, None)

    async def ready(self):
        return self.up


@pytest.fixture
async def migration_db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(MDataset.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids = [uuid.uuid4() for _ in range(4)]
    async with factory() as session:
        session.add_all(
            [
                MDataset(id=ids[0], name="ok", storage_backend="hdfs", hdfs_path=f"/platform/raw/{ids[0]}.csv",
                         file_format="csv", created_by="alice", validation_status="pending"),
                MDataset(id=ids[1], name="missing", storage_backend="hdfs", hdfs_path=f"/platform/raw/{ids[1]}.csv",
                         file_format="csv", created_by="bob", validation_status="pending"),
                MDataset(id=ids[2], name="wrong-size", storage_backend="hdfs", hdfs_path=f"/platform/raw/{ids[2]}.parquet",
                         file_format="parquet", created_by="bob", validation_status="pending", size_bytes=999),
                MDataset(id=ids[3], name="already-local", storage_backend="local", hdfs_path=f"raw/{ids[3]}.csv",
                         file_format="csv", created_by="alice", validation_status="pending"),
            ]
        )
        await session.commit()
    yield factory, ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_moves_good_rows_and_leaves_bad_ones_untouched(migration_db, tmp_path):
    factory, ids = migration_db
    hdfs = MemHdfs({
        f"/platform/raw/{ids[0]}.csv": b"a\n1\n",
        f"/platform/raw/{ids[2]}.parquet": b"PAR1",
    })
    local = mblob.LocalFileStore(tmp_path)
    local.prepare()
    out = io.StringIO()

    async with factory() as session:
        assert await migrate_mod.migrate_rows(session, local, hdfs, 10_000, out=out) == (1, 2)

    async with factory() as session:
        rows = {d.name: d for d in (await session.execute(migrate_mod.select(MDataset))).scalars()}
    assert (rows["ok"].storage_backend, rows["ok"].hdfs_path, rows["ok"].size_bytes) == ("local", f"raw/{ids[0]}.csv", 4)
    assert (tmp_path / "raw" / f"{ids[0]}.csv").read_bytes() == b"a\n1\n"
    for name in ("missing", "wrong-size"):
        assert rows[name].storage_backend == "hdfs"
    assert rows["already-local"].hdfs_path == f"raw/{ids[3]}.csv"
    # Failed rows leave no file behind, and HDFS keeps its copies.
    assert sorted(p.name for p in (tmp_path / "raw").iterdir()) == [f"{ids[0]}.csv"]
    assert f"/platform/raw/{ids[0]}.csv" in hdfs.files
    assert "FAILED" in out.getvalue() and "size mismatch" in out.getvalue()

    # Running it again only retries what is still in HDFS.
    async with factory() as session:
        assert await migrate_mod.migrate_rows(session, local, hdfs, 10_000, out=io.StringIO()) == (0, 2)


@pytest.mark.asyncio
async def test_migration_dry_run_and_hdfs_down_change_nothing(migration_db, tmp_path):
    factory, _ = migration_db
    local = mblob.LocalFileStore(tmp_path)
    local.prepare()
    async with factory() as session:
        assert await migrate_mod.migrate_rows(session, local, MemHdfs({}), 10_000, dry_run=True, out=io.StringIO()) == (0, 0)
        out = io.StringIO()
        assert await migrate_mod.migrate_rows(session, local, MemHdfs({}, up=False), 10_000, out=out) == (0, 3)
        assert "make up-hdfs" in out.getvalue()
    async with factory() as session:
        backends = [d.storage_backend for d in (await session.execute(migrate_mod.select(MDataset))).scalars()]
    assert sorted(backends) == ["hdfs", "hdfs", "hdfs", "local"]
    assert list((tmp_path / "raw").iterdir()) == []


# --- streamed download through the real routes ---------------------------------

def _token(sub):
    s = get_base_settings()
    import time
    now = int(time.time())
    return jwt.encode({"sub": sub, "iat": now, "exp": now + 600}, s.jwt_secret, algorithm=s.jwt_algorithm)


@pytest.fixture
async def real_api(tmp_path):
    # A file, not ":memory:": the test client uses its own connections.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Dataset.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    local = blob_store.LocalFileStore(tmp_path / "data")
    local.prepare()
    settings = SimpleNamespace(storage_backend="local", max_upload_size_bytes=50, hdfs_base_path="/platform")

    def service():
        session = factory()
        return DatasetService(session, {"local": local, "hdfs": MemHdfs({}, up=False)}, settings)

    app = FastAPI()
    from shared.common.error_handlers import platform_exception_handler
    from shared.common.exceptions import PlatformException
    app.add_exception_handler(PlatformException, platform_exception_handler)
    app.include_router(routes_mod.router, prefix="/api/v1/storage")
    app.dependency_overrides[routes_mod.get_dataset_service] = service
    with TestClient(app) as client:
        yield client, factory, tmp_path / "data"
    await engine.dispose()


@pytest.mark.asyncio
async def test_upload_download_delete_through_the_routes(real_api):
    client, factory, root = real_api
    alice = {"Authorization": f"Bearer {_token('alice')}"}
    r = client.post("/api/v1/storage/datasets", headers=alice, data={"name": "pts"},
                    files={"file": ("pts.csv", b"x,label\n1,0\n", "text/csv")})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["storage_backend"] == "local"
    assert body["storage_path"] == body["hdfs_path"] == f"raw/{body['id']}.csv"
    assert body["size_bytes"] == 12

    r = client.get(f"/api/v1/storage/datasets/{body['id']}/download", headers=alice)
    assert r.status_code == 200
    assert r.content == b"x,label\n1,0\n"
    assert r.headers["content-length"] == "12"
    assert r.headers["content-type"].startswith("text/csv")

    bob = {"Authorization": f"Bearer {_token('bob')}"}
    assert client.get(f"/api/v1/storage/datasets/{body['id']}/download", headers=bob).status_code == 404

    assert client.delete(f"/api/v1/storage/datasets/{body['id']}", headers=alice).status_code == 204
    assert list((root / "raw").iterdir()) == []


@pytest.mark.asyncio
async def test_routes_report_size_limit_and_hdfs_down(real_api):
    client, factory, root = real_api
    alice = {"Authorization": f"Bearer {_token('alice')}"}
    r = client.post("/api/v1/storage/datasets", headers=alice, data={"name": "big"},
                    files={"file": ("big.csv", b"x" * 51, "text/csv")})
    assert r.status_code == 413
    assert r.json()["error_code"] == "FILE_TOO_LARGE"
    assert list((root / "raw").iterdir()) == []

    old = uuid.uuid4()
    async with factory() as session:
        session.add(Dataset(id=old, name="old", storage_backend="hdfs", hdfs_path=f"/platform/raw/{old}.csv",
                            file_format="csv", created_by="alice", validation_status="pending"))
        await session.commit()
    r = client.get(f"/api/v1/storage/datasets/{old}/download", headers=alice)
    assert r.status_code == 503
    assert "make migrate-datasets" in r.json()["message"]
