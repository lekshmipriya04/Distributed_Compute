"""Projects: bundles, the worker helper, the API, and a run through the real
sandbox runner on this machine (POSIX)."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared.common.auth import get_current_user

_SVC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "services", "ssh-executor")
)
if _SVC not in sys.path:
    sys.path.insert(0, _SVC)

import projects  # noqa: E402
from projects import BundleError, BundleStore, BundleTooLarge, ProjectRun, validate_bundle  # noqa: E402
from runtimes import ResourceSpec  # noqa: E402

OWNER = "user-1"


def make_bundle(files: dict[str, bytes | str], *, extra=None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return buf.getvalue()


def write(tmp_path, data: bytes, name="b.tar.gz") -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


async def _chunks(data: bytes, size=7):
    for i in range(0, len(data), size):
        yield data[i:i + size]


# --- paths and bundles -----------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("outputs", "outputs"), ("./outputs/", "outputs"), ("data/train.csv", "data/train.csv"),
])
def test_relative_paths_are_normalised(value, expected):
    assert projects.check_relative_path(value, "x") == expected


@pytest.mark.parametrize("value", ["", "/etc", "../up", "a/../b", "a//b", "a b", "~/x", "a/./b"])
def test_paths_outside_the_project_are_refused(value):
    with pytest.raises(ValueError):
        projects.check_relative_path(value, "x")


def test_validate_bundle_counts_files(tmp_path):
    path = write(tmp_path, make_bundle({"main.py": "print(1)", "src/lib.py": "X = 1"}))
    assert validate_bundle(path, max_unpacked_bytes=10_000, max_members=10) == {"files": 2, "unpacked_bytes": 13}


def test_validate_bundle_refuses_traversal_links_and_bombs(tmp_path):
    with pytest.raises(BundleError, match="Unsafe path"):
        validate_bundle(write(tmp_path, make_bundle({"../evil.py": "x"})), max_unpacked_bytes=100, max_members=10)
    with pytest.raises(BundleError, match="Unsafe path"):
        validate_bundle(write(tmp_path, make_bundle({"/etc/x": "x"})), max_unpacked_bytes=100, max_members=10)
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(BundleError, match="link or device"):
        validate_bundle(write(tmp_path, make_bundle({"a.py": "x"}, extra=[link])), max_unpacked_bytes=100,
                        max_members=10)
    with pytest.raises(BundleTooLarge):
        validate_bundle(write(tmp_path, make_bundle({"big": b"0" * 5000})), max_unpacked_bytes=1000, max_members=10)
    with pytest.raises(BundleTooLarge):
        validate_bundle(write(tmp_path, make_bundle({f"f{i}": "x" for i in range(5)})), max_unpacked_bytes=1000,
                        max_members=3)
    with pytest.raises(BundleError, match="not a valid"):
        validate_bundle(write(tmp_path, b"not a tar"), max_unpacked_bytes=100, max_members=10)
    with pytest.raises(BundleError, match="empty"):
        validate_bundle(write(tmp_path, make_bundle({})), max_unpacked_bytes=100, max_members=10)


def test_bundle_store_keys_by_owner_and_hash_and_leaves_nothing_on_failure(tmp_path):
    store = BundleStore(tmp_path)
    data = make_bundle({"main.py": "print(1)"})
    limits = dict(max_bytes=1_000_000, max_unpacked_bytes=1_000_000, max_members=100)
    info = asyncio.run(store.receive(OWNER, _chunks(data), **limits))
    assert info["id"] == hashlib.sha256(data).hexdigest()
    assert store.exists(OWNER, info["id"])
    assert not store.exists("someone-else", info["id"])
    assert not store.exists(OWNER, "not-a-hash")
    with pytest.raises(BundleTooLarge):
        asyncio.run(store.receive(OWNER, _chunks(data), **{**limits, "max_bytes": 10}))
    with pytest.raises(BundleError):
        asyncio.run(store.receive(OWNER, _chunks(b"garbage"), **limits))
    leftovers = [p.name for p in tmp_path.rglob("*") if p.is_file() and p.name.startswith(".upload-")]
    assert leftovers == []


def test_project_spec_validation():
    from schemas import BatchCreateRequest, ProjectSpec

    req = BatchCreateRequest(project={"bundle_id": "a" * 64, "command": "python main.py"})
    assert req.runtime == "project"
    assert req.project.outputs == ["outputs"]
    spec = ProjectSpec(bundle_id="b" * 64, command="x", outputs=["out/", "out", "models"],
                       requirements="./requirements.txt",
                       datasets=[{"dataset_id": str(uuid.uuid4()), "path": "data/t.csv"}])
    assert spec.outputs == ["out", "models"]
    assert spec.requirements == "requirements.txt"
    for bad in ({"bundle_id": "short", "command": "x"},
                {"bundle_id": "a" * 64, "command": "   "},
                {"bundle_id": "a" * 64, "command": "x", "outputs": ["../x"]},
                {"bundle_id": "a" * 64, "command": "x", "requirements": "/etc/passwd"}):
        with pytest.raises(ValueError):
            ProjectSpec(**bad)
    with pytest.raises(ValueError):
        BatchCreateRequest(runtime="project")


# --- API -------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch, tmp_path):
    import routes as rb

    bundles = projects.BundleStore(tmp_path / "bundles")
    outputs = projects.OutputStore(tmp_path / "outputs")
    monkeypatch.setattr(rb, "project_stores", lambda settings: (bundles, outputs, str(tmp_path / "datasets")))
    nodes = [SimpleNamespace(id=uuid.uuid4(), status="online", os_type="linux",
                             runtimes_json='{"python": "3.11"}', python_version="3.11")]

    async def fake_online(session, owner_id, node_ids=None):
        return nodes

    monkeypatch.setattr(rb.execution_manager, "online_nodes", fake_online)
    dispatched = []
    monkeypatch.setattr(rb, "dispatch_batch", lambda *a: dispatched.append(a))
    batch_id = uuid.uuid4()
    task = SimpleNamespace(id=uuid.uuid4(), seq=0, output_files=0)
    created = []

    class Service:
        async def create_batch(self, request, owner_id, *, timeout_seconds):
            created.append(request)
            from datetime import datetime, timezone
            return SimpleNamespace(
                id=batch_id, owner_id=owner_id, runtime=request.runtime, status="pending", message=None,
                total=1, completed=0, failed=0, cancelled=0, cpu_per_task=1, timeout_seconds=timeout_seconds,
                created_at=datetime.now(timezone.utc), finished_at=None,
                project_json=request.project.model_dump_json() if request.project else None,
            )

        async def get_batch(self, bid, owner_id):
            return SimpleNamespace(id=batch_id) if bid == batch_id and owner_id == OWNER else None

        async def list_tasks(self, bid):
            return [task]

    async def no_db():
        yield None

    app = FastAPI()
    app.include_router(rb.router, prefix="/api/v1/ssh")
    user = {"sub": OWNER}
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[rb.get_db_session] = no_db
    app.dependency_overrides[rb.get_batch_service] = lambda: Service()
    return SimpleNamespace(client=TestClient(app), nodes=nodes, dispatched=dispatched, created=created,
                           batch_id=batch_id, task=task, outputs=outputs, user=user)


def test_upload_check_and_run_a_project(api):
    data = make_bundle({"main.py": "print(1)"})
    sha = hashlib.sha256(data).hexdigest()
    assert api.client.get(f"/api/v1/ssh/bundles/{sha}").status_code == 404
    r = api.client.post("/api/v1/ssh/bundles", content=data, headers={"Content-Type": "application/gzip"})
    assert r.status_code == 201, r.text
    assert r.json() == {"id": sha, "size_bytes": len(data), "files": 1, "unpacked_bytes": 8}
    assert api.client.get(f"/api/v1/ssh/bundles/{sha}").json()["id"] == sha
    r = api.client.post("/api/v1/ssh/batches", json={
        "inputs": ["1", "2"], "project": {"bundle_id": sha, "command": "python3 main.py"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["runtime"] == "project"
    assert body["project"]["command"] == "python3 main.py"
    assert "project_json" not in body
    assert len(api.dispatched) == 1
    # Another user cannot use, or even see, this bundle.
    api.user["sub"] = "user-2"
    assert api.client.get(f"/api/v1/ssh/bundles/{sha}").status_code == 404
    r = api.client.post("/api/v1/ssh/batches", json={"project": {"bundle_id": sha, "command": "x"}})
    assert r.status_code == 400 and "Unknown bundle_id" in r.json()["detail"]


def test_bad_bundles_and_project_requests_are_refused(api):
    assert api.client.post("/api/v1/ssh/bundles", content=b"nope").status_code == 400
    sha = api.client.post("/api/v1/ssh/bundles", content=make_bundle({"a.py": "x"})).json()["id"]
    r = api.client.post("/api/v1/ssh/batches", json={"project": {"bundle_id": sha, "command": "x"},
                                                     "timeout_seconds": 10 ** 7})
    assert r.status_code == 400 and "exceeds the maximum" in r.json()["detail"]
    for node in api.nodes:
        node.os_type = "windows"
    r = api.client.post("/api/v1/ssh/batches", json={"project": {"bundle_id": sha, "command": "x"}})
    assert r.status_code == 400 and "Linux or macOS" in r.json()["detail"]
    assert api.created == []


def test_outputs_download(api):
    url = f"/api/v1/ssh/batches/{api.batch_id}/tasks/{api.task.id}/outputs"
    assert api.client.get(url).status_code == 404
    api.outputs.root.mkdir(parents=True)
    api.outputs.path(str(api.task.id)).write_bytes(b"archive")
    api.task.output_files = 2
    r = api.client.get(url)
    assert r.status_code == 200 and r.content == b"archive"
    assert r.headers["content-type"] == "application/gzip"
    assert f"outputs-{str(api.batch_id)[:8]}-task1.tar.gz" in r.headers["content-disposition"]
    assert api.client.get(f"/api/v1/ssh/batches/{uuid.uuid4()}/tasks/{api.task.id}/outputs").status_code == 404
    api.user["sub"] = "user-2"
    assert api.client.get(url).status_code == 404


# --- a real run on this machine ----------------------------------------------------

class LocalTransport:
    """SSHTransport on this host, with HOME pointed at a temporary folder."""

    host = "localhost"
    is_connected = True

    def __init__(self, home: str):
        self.home = home

    def connect(self):
        return {}

    def close(self):
        pass

    def exec(self, command, *, input_data=None, timeout=None, max_output_bytes=None):
        env = dict(os.environ, HOME=self.home)
        proc = subprocess.run(["/bin/sh", "-c", command], input=(input_data or "").encode(), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return {"exit_code": proc.returncode, "stdout": proc.stdout.decode("utf-8", "replace"),
                "stderr": proc.stderr.decode("utf-8", "replace")}

    def put_text(self, path, content):
        with open(path, "w") as fh:
            fh.write(content)

    def upload(self, local, remote):
        shutil.copyfile(local, remote)

    def fetch(self, remote, local):
        shutil.copyfile(remote, local)


PROJECT_FILES = {
    "main.py": (
        "import json, os, sys\n"
        "from src.model import double\n"
        "rows = open(os.path.join(os.environ['DC_DATA_DIR'], 'nums.csv')).read().split()\n"
        "value = sum(double(int(r)) for r in rows) + int(sys.stdin.read() or 0)\n"
        "os.makedirs(os.path.join(os.environ['DC_OUTPUT_DIR'], 'nested'), exist_ok=True)\n"
        "json.dump({'value': value, 'task': os.environ['DC_TASK_INDEX'], 'venv': os.environ.get('VIRTUAL_ENV')},\n"
        "          open(os.path.join(os.environ['DC_OUTPUT_DIR'], 'result.json'), 'w'))\n"
        "open(os.path.join(os.environ['DC_OUTPUT_DIR'], 'nested', 'log.txt'), 'w').write('ok')\n"
        "print('value', value, 'python', sys.executable)\n"
        "sys.exit(int(os.environ.get('FAIL', '0')))\n"
    ),
    "src/__init__.py": "",
    "src/model.py": "def double(x):\n    return 2 * x\n",
    "requirements.txt": "# nothing to install: keeps the test offline\n",
    "run.sh": "#!/bin/sh\necho shell $1\n",
}


def _adapter():
    from os_adapters import LinuxAdapter, MacOSAdapter

    return MacOSAdapter() if sys.platform == "darwin" else LinuxAdapter()


def _members(path):
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


@pytest.mark.skipif(os.name == "nt", reason="POSIX sandbox runner")
class TestProjectRun:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        from tasks import TaskExecutor

        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.executor = TaskExecutor(LocalTransport(str(self.home)), _adapter())
        self.spec = ResourceSpec(cpu_cores=1, memory_gb=1.0, timeout_seconds=120)
        self.bundle = write(tmp_path, make_bundle(PROJECT_FILES))
        data = tmp_path / "nums.csv"
        data.write_text("1\n2\n3\n")
        self.data = str(data)

    def run(self, **overrides):
        out = str(self.tmp / f"out-{uuid.uuid4().hex}.tar.gz")
        kwargs = dict(bundle_path=self.bundle, command="python3 main.py", output_path=out,
                      datasets=[(self.data, "data/nums.csv")])
        kwargs.update(overrides)
        project = ProjectRun(**kwargs)
        result = self.executor.run("python", "", "10", self.spec, project=project, task_index=1, task_count=3)
        return result, out

    def test_runs_the_folder_with_data_and_brings_back_outputs(self):
        result, out = self.run()
        assert result["status"] == "success", result["stderr"]
        assert "value 22" in result["stdout"]
        assert result["output_files"] == 2
        files = _members(out)
        assert set(files) == {"outputs/result.json", "outputs/nested/log.txt"}
        assert json.loads(files["outputs/result.json"]) == {"value": 22, "task": "1", "venv": None}
        assert not [d for d in os.listdir(tempfile.gettempdir()) if d.startswith("dp-exec.") and
                    result["execution_id"] in d], "sandbox must be deleted"

    def test_failed_command_still_brings_back_outputs(self):
        result, out = self.run(extra_env={"FAIL": "3"})
        assert result["status"] == "error" and result["exit_code"] == 3
        assert result["output_files"] == 2 and os.path.isfile(out)

    def test_other_commands_and_no_outputs(self):
        result, out = self.run(command="sh run.sh hello", outputs=[])
        assert result["exit_code"] == 0 and "shell hello" in result["stdout"]
        assert result["output_files"] is None and not os.path.exists(out)

    def test_output_limit_leaves_files_out_and_says_so(self):
        result, out = self.run(max_output_bytes=5)
        assert result["output_files"] == 1
        assert "left out, over the" in result["stderr"]

    def test_environment_in_the_sandbox_is_not_kept(self):
        result, _ = self.run(requirements="requirements.txt")
        assert result["status"] == "success", result["stderr"]
        assert "/.dc-env/bin/python" in result["stdout"]
        assert "it is deleted after the task" in result["stderr"]
        assert not (self.home / ".cache").exists(), "nothing may be left on the worker"

    def test_cached_environment_is_reused_and_removed_with_the_node(self):
        first, _ = self.run(requirements="requirements.txt", env_cache_bytes=10 * 2**30)
        assert first["status"] == "success", first["stderr"]
        assert "cached on this node" in first["stderr"]
        envs = self.home / ".cache" / "distributed-compute" / "envs"
        assert len([p for p in envs.iterdir() if p.is_dir()]) == 1
        second, out = self.run(requirements="requirements.txt", env_cache_bytes=10 * 2**30)
        assert "reused the cached one" in second["stderr"]
        assert str(envs) in json.loads(_members(out)["outputs/result.json"])["venv"]

        from node_service import _remove_worker_cache

        _remove_worker_cache(self.executor.transport)
        assert not (self.home / ".cache" / "distributed-compute").exists()

    def test_a_failed_environment_build_is_a_task_failure_with_the_log(self):
        bundle = write(self.tmp, make_bundle({**PROJECT_FILES, "requirements.txt": "-r missing.txt\n"}), "bad.tar.gz")
        result, _ = self.run(bundle_path=bundle, requirements="requirements.txt", env_cache_bytes=2**30)
        assert result["status"] == "error" and result["exit_code"] not in (None, 0)
        assert "[dc] Building the environment failed" in result["stderr"]
        assert "missing.txt" in result["stderr"]
        envs = self.home / ".cache" / "distributed-compute" / "envs"
        assert [p for p in envs.iterdir() if p.is_dir()] == [], "a failed build must not stay cached"

    def test_missing_requirements_file(self):
        result, _ = self.run(requirements="nope.txt")
        assert result["exit_code"] not in (None, 0)
        assert "nope.txt is not in the project" in result["stderr"]

    def test_timeout_still_collects_outputs(self):
        spec = ResourceSpec(cpu_cores=1, memory_gb=1.0, timeout_seconds=2)
        out = str(self.tmp / "t.tar.gz")
        project = ProjectRun(bundle_path=self.bundle, output_path=out,
                             command="mkdir -p outputs && echo partial > outputs/p.txt && sleep 30")
        result = self.executor.run("python", "", "", spec, project=project)
        assert result["exit_code"] == 124 and "[task timed out]" in result["stderr"]
        assert _members(out) == {"outputs/p.txt": b"partial\n"}


# --- dispatcher setup ----------------------------------------------------------------

def test_prepare_project_checks_the_bundle_and_sizes_the_cache_per_node(tmp_path, monkeypatch):
    import tasks
    from schemas import ProjectSpec

    settings = SimpleNamespace(project_data_dir=str(tmp_path), env_build_timeout_seconds=99,
                               max_project_output_bytes=1234)
    bundles, outputs, _ = tasks.project_stores(settings)
    data = make_bundle({"main.py": "print(1)"})
    sha = asyncio.run(bundles.receive(OWNER, _chunks(data), max_bytes=10**6, max_unpacked_bytes=10**6,
                                      max_members=10))["id"]
    spec = ProjectSpec(bundle_id=sha, command="python3 main.py", requirements="requirements.txt")
    batch = SimpleNamespace(owner_id=OWNER, project_json=spec.model_dump_json())
    task_list = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    outputs.root.mkdir(parents=True)
    stale = outputs.path(str(task_list[0].id))
    stale.write_bytes(b"old")

    project_for = asyncio.run(tasks._prepare_project(batch, task_list, settings))
    assert not stale.exists(), "outputs of an earlier attempt are removed"
    run = project_for(1, SimpleNamespace(env_cache_gb=2.5))
    assert run.bundle_path == str(bundles.path(OWNER, sha))
    assert run.output_path == str(outputs.path(str(task_list[1].id)))
    assert run.env_cache_bytes == int(2.5 * 1024 ** 3)
    assert (run.env_timeout_seconds, run.max_output_bytes, run.requirements) == (99, 1234, "requirements.txt")
    assert project_for(0, SimpleNamespace(env_cache_gb=0)).env_cache_bytes == 0

    other = SimpleNamespace(owner_id="someone-else", project_json=spec.model_dump_json())
    with pytest.raises(tasks.ProjectSetupError, match="no longer on the master"):
        asyncio.run(tasks._prepare_project(other, task_list, settings))


def test_project_nodes_need_python_and_posix():
    import tasks

    def node(os_type, runtimes):
        return SimpleNamespace(id=uuid.uuid4(), status="online", os_type=os_type,
                               runtimes_json=json.dumps(runtimes), python_version=runtimes.get("python"))

    linux, mac, win, nopy = node("linux", {"python": "3.12"}), node("macos", {"python": "3.11"}), \
        node("windows", {"python": "3.12"}), node("linux", {"bash": "5"})
    nopy.python_version = None
    assert tasks.project_nodes([linux, mac, win, nopy]) == [linux, mac]
