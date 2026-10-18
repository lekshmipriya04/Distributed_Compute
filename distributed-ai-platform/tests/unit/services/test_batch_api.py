"""API tests for ssh-executor runtimes/batches routes (no DB, no SSH)."""
from __future__ import annotations

import importlib
import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shared.common.auth import get_current_user

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SVC = os.path.join(_ROOT, "services", "ssh-executor")

_MODS: dict = {}

def _get(name: str):
    """Lazy-load ssh-executor modules on first use, not at collection time."""
    if not _MODS:
        for p in (_ROOT, _SVC):
            if p not in sys.path:
                sys.path.insert(0, p)
        for mod in ("config", "models", "database", "schemas", "security", "ssh",
                    "node_service", "tasks", "leases", "routes"):
            # Reuse already-loaded ssh-executor modules; re-import only if absent
            # or replaced by another service's module.
            cached = sys.modules.get(mod)
            if cached is None or _SVC not in (getattr(cached, "__file__", "") or ""):
                # Remove stale copy before re-importing
                sys.modules.pop(mod, None)
                _MODS[mod] = importlib.import_module(mod)
            else:
                _MODS[mod] = cached
    return _MODS[name]


def _rb():
    return _get("routes")

def _rc():
    return _get("routes")

OWNER = "user-1"


def _batch(status="running", owner=OWNER):
    return SimpleNamespace(
        id=uuid.uuid4(), owner_id=owner, runtime="python", status=status, message=None,
        total=2, completed=0, failed=0, cancelled=0, cpu_per_task=1, timeout_seconds=120,
        created_at=datetime.now(timezone.utc), finished_at=None, project_json=None,
    )


class FakeService:
    def __init__(self):
        self.batches = {}
        self.created = []
        self.cancel_calls = 0

    async def create_batch(self, request, owner_id, *, timeout_seconds):
        b = _batch("pending", owner_id)
        b.timeout_seconds = timeout_seconds
        self.batches[b.id] = b
        self.created.append((request, timeout_seconds))
        return b

    async def get_batch(self, batch_id, owner_id):
        b = self.batches.get(batch_id)
        return b if b and b.owner_id == owner_id else None

    async def list_batches(self, owner_id, limit=20):
        return [b for b in self.batches.values() if b.owner_id == owner_id][:limit]

    async def request_cancel(self, batch):
        self.cancel_calls += 1
        if batch.status in ("pending", "running"):
            batch.status = "cancelling"
        return batch


@pytest.fixture
def ctx(monkeypatch):
    svc = FakeService()
    n_py = SimpleNamespace(id=uuid.uuid4(), status="online", runtimes_json='{"python": "3.11", "go": "1.22"}', python_version="3.11")
    n_legacy = SimpleNamespace(id=uuid.uuid4(), status="online", runtimes_json=None, python_version="3.9")
    nodes = [n_py, n_legacy]
    seen_owner = []

    async def fake_online(session, owner_id, node_ids=None):
        seen_owner.append(owner_id)
        return nodes

    rb = _rb()
    rc = _rc()
    monkeypatch.setattr(rb.execution_manager, "online_nodes", fake_online)
    dispatched = []
    monkeypatch.setattr(rb, "dispatch_batch", lambda *a: dispatched.append(a))

    async def no_db():
        yield None

    app = FastAPI()
    app.include_router(rb.router, prefix="/api/v1/ssh")
    app.dependency_overrides[get_current_user] = lambda: {"sub": OWNER}
    app.dependency_overrides[rc.get_db_session] = no_db
    app.dependency_overrides[rb.get_batch_service] = lambda: svc
    return SimpleNamespace(client=TestClient(app), svc=svc, n_py=n_py, n_legacy=n_legacy,
                           dispatched=dispatched, seen_owner=seen_owner)


def test_runtimes_lists_available_nodes(ctx):
    r = ctx.client.get("/api/v1/ssh/runtimes")
    assert r.status_code == 200
    items = {i["id"]: i for i in r.json()["items"]}
    assert set(items["python"]["available_node_ids"]) == {str(ctx.n_py.id), str(ctx.n_legacy.id)}
    assert items["go"]["available_node_ids"] == [str(ctx.n_py.id)]
    assert len(items["custom"]["available_node_ids"]) == 2
    assert ctx.seen_owner == [OWNER]


def test_create_batch_ok_and_dispatches(ctx):
    r = ctx.client.post("/api/v1/ssh/batches", json={"code": "print(1)", "inputs": ["a", "b"], "timeout_seconds": 30})
    assert r.status_code == 201, r.text
    assert ctx.svc.created[0][1] == 30
    assert len(ctx.dispatched) == 1


@pytest.mark.parametrize("body", [
    {"code": "x", "timeout_seconds": 999999},
    {"code": "x", "runtime": "nope"},
    {"code": "x", "runtime": "custom", "custom_command": "run it", "custom_filename": "a.x"},
    {"code": "x", "custom_command": "python {file}"},
    {"code": "   "},
])
def test_create_batch_validation(ctx, body):
    r = ctx.client.post("/api/v1/ssh/batches", json=body)
    assert r.status_code == 400, r.text
    assert not ctx.svc.created


def test_create_custom_ok(ctx):
    r = ctx.client.post("/api/v1/ssh/batches", json={
        "code": "x", "runtime": "custom", "custom_command": "sh {file}", "custom_filename": "t.sh"})
    assert r.status_code == 201, r.text


def test_list_batches(ctx):
    for _ in range(25):
        b = _batch()
        ctx.svc.batches[b.id] = b
    other = _batch(owner="someone-else")
    ctx.svc.batches[other.id] = other
    r = ctx.client.get("/api/v1/ssh/batches")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 20
    assert all(i["owner_id"] == OWNER for i in items)


def test_cancel_running_and_terminal_and_not_owner(ctx):
    running, done, foreign = _batch("running"), _batch("completed"), _batch(owner="x")
    for b in (running, done, foreign):
        ctx.svc.batches[b.id] = b
    r = ctx.client.post(f"/api/v1/ssh/batches/{running.id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelling"
    r = ctx.client.post(f"/api/v1/ssh/batches/{done.id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "completed"
    r = ctx.client.post(f"/api/v1/ssh/batches/{foreign.id}/cancel")
    assert r.status_code == 404
    assert ctx.client.post(f"/api/v1/ssh/batches/{uuid.uuid4()}/cancel").status_code == 404
    assert ctx.svc.cancel_calls == 2


def test_create_batch_rejects_runtime_no_node_has(ctx):
    # Contract §2.2: 400 naming the runtime, before anything is stored.
    r = ctx.client.post("/api/v1/ssh/batches", json={"code": "fn main() {}", "runtime": "rust"})
    assert r.status_code == 400, r.text
    assert "rust" in r.json()["detail"]
    assert not ctx.svc.created and not ctx.dispatched


def test_create_batch_with_runtime_on_one_node_ok(ctx):
    r = ctx.client.post("/api/v1/ssh/batches", json={"code": "package main", "runtime": "go"})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_batch_service_stores_execution_policy():
    schemas = _get("schemas")
    tasks_mod = _get("tasks")

    class FakeSession:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            self.added[0].id = uuid.uuid4()

        async def commit(self):
            pass

        async def refresh(self, obj):
            pass

    session = FakeSession()
    req = schemas.BatchCreateRequest(code="print(1)", inputs=["a"], execution_policy="max_available")
    batch = await tasks_mod.BatchService(session).create_batch(req, OWNER, timeout_seconds=30)
    assert batch.execution_policy == "max_available"


def _finished_batch(ctx, status="completed_with_errors", task_statuses=("completed", "failed", "cancelled")):
    b = _batch(status)
    b.code = "print(input())"
    b.custom_command = None
    b.custom_filename = None
    b.execution_policy = "balanced"
    b.node_ids_json = f'["{ctx.n_py.id}"]'
    ctx.svc.batches[b.id] = b
    tasks = [
        SimpleNamespace(seq=i, input_data=f"in-{i}", status=s)
        for i, s in enumerate(task_statuses)
    ]

    async def list_tasks(batch_id):
        return tasks if batch_id == b.id else []

    ctx.svc.list_tasks = list_tasks
    return b


def test_retry_reruns_every_input_as_a_new_batch(ctx):
    b = _finished_batch(ctx)
    r = ctx.client.post(f"/api/v1/ssh/batches/{b.id}/retry")
    assert r.status_code == 201, r.text
    assert r.json()["id"] != str(b.id)
    request, timeout = ctx.svc.created[-1]
    assert request.inputs == ["in-0", "in-1", "in-2"]
    assert request.code == "print(input())"
    assert request.execution_policy == "balanced"
    assert [str(n) for n in request.node_ids] == [str(ctx.n_py.id)]
    assert timeout == 120
    assert len(ctx.dispatched) == 1


def test_retry_failed_only_keeps_failed_and_cancelled_inputs(ctx):
    b = _finished_batch(ctx)
    r = ctx.client.post(f"/api/v1/ssh/batches/{b.id}/retry?failed_only=true")
    assert r.status_code == 201
    assert ctx.svc.created[-1][0].inputs == ["in-1", "in-2"]


def test_retry_refuses_running_batches_and_nothing_to_retry(ctx):
    running = _finished_batch(ctx, status="running")
    assert ctx.client.post(f"/api/v1/ssh/batches/{running.id}/retry").status_code == 409
    clean = _finished_batch(ctx, status="completed", task_statuses=("completed",))
    r = ctx.client.post(f"/api/v1/ssh/batches/{clean.id}/retry?failed_only=true")
    assert r.status_code == 400
    assert ctx.client.post(f"/api/v1/ssh/batches/{uuid.uuid4()}/retry").status_code == 404


def test_node_update_accepts_only_online_or_draining():
    schemas = _get("schemas")
    assert schemas.NodeUpdateRequest(status="draining").status == "draining"
    assert schemas.NodeUpdateRequest().status is None
    with pytest.raises(Exception):
        schemas.NodeUpdateRequest(status="offline")
