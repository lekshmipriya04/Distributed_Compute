"""dc, the command-line client: packing, and a run against a fake gateway."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "cli"))

from distributed_compute import cli  # noqa: E402
from distributed_compute.packing import is_excluded, pack, read_project_block, safe_extract  # noqa: E402


def make_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "train.py").write_text("print('hi')\n")
    (root / "src" / "model.py").write_text("X = 1\n")
    (root / "requirements.txt").write_text("numpy\n")
    (root / "run.sh").write_text("#!/bin/sh\necho ok\n")
    for junk in (".git/config", "outputs/old.bin", ".venv/bin/python", "src/__pycache__/m.pyc", "data/raw/big.csv"):
        (root / junk).parent.mkdir(parents=True, exist_ok=True)
        (root / junk).write_text("junk")


def members(archive: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {m.name: m for m in tar}


def test_excludes_match_the_extension():
    for path in (".git/config", "node_modules/x/i.js", "a/__pycache__/b.pyc", "pkg/venv/lib/x.py",
                 "outputs/model.pt", "x.pyc", "sub/.DS_Store"):
        assert is_excluded(path, cli.pack.__globals__["DEFAULT_EXCLUDES"]), path
    for path in ("train.py", "configs/outputs.yaml", "venv.md", "myoutputs/x"):
        assert not is_excluded(path, cli.pack.__globals__["DEFAULT_EXCLUDES"]), path
    patterns = ["data/raw/", "*.ckpt", "/notes.txt"]
    assert is_excluded("data/raw/a.csv", patterns) and not is_excluded("data/clean.csv", patterns)
    assert is_excluded("m/best.ckpt", patterns)
    assert is_excluded("notes.txt", patterns) and not is_excluded("sub/notes.txt", patterns)


def test_pack_is_deterministic_and_leaves_out_junk(tmp_path):
    make_project(tmp_path)
    first = pack(tmp_path, ["data/raw/"])
    assert sorted(members(first.archive)) == ["requirements.txt", "run.sh", "src/model.py", "train.py"]
    assert members(first.archive)["run.sh"].mode == 0o755
    os.utime(tmp_path / "train.py", (1, 1))  # a new timestamp alone does not change the archive
    assert pack(tmp_path, ["data/raw/"]).sha256 == first.sha256
    (tmp_path / "train.py").write_text("print('changed')\n")
    assert pack(tmp_path, ["data/raw/"]).sha256 != first.sha256


def test_pack_uses_gitignore_in_a_repository(tmp_path):
    make_project(tmp_path)
    if subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=False).returncode != 0:
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    (repo / "keep.py").write_text("x")
    (repo / "secret.env").write_text("x")
    (repo / ".gitignore").write_text("*.env\n")
    assert sorted(pack(repo).files) == [".gitignore", "keep.py"]


def test_project_block_and_safe_extract(tmp_path):
    assert read_project_block(tmp_path) == {}
    (tmp_path / "distributed.json").write_text(json.dumps({"version": 1, "project": {"command": "make"}}))
    assert read_project_block(tmp_path) == {"command": "make"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe path"):
        safe_extract(buf.getvalue(), tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


class FakeGateway(BaseHTTPRequestHandler):
    """Just enough of the extension gateway for one run."""

    state: dict = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        s = self.state
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        s["calls"].append(("POST", self.path, self.headers.get("Authorization")))
        if self.path == "/auth/local-token":
            return self._send(200, {"token": "local-tok"})
        if self.path == "/bundles":
            s["bundle"] = body
            return self._send(201, {"id": hashlib.sha256(body).hexdigest(), "size_bytes": len(body)})
        if self.path == "/executions":
            s["request"] = json.loads(body)
            return self._send(201, {"id": "e1", "runtime": "project", "status": "pending", "total": 2})
        self._send(404, {"error": "nope"})

    def do_GET(self):
        s = self.state
        s["calls"].append(("GET", self.path, self.headers.get("Authorization")))
        if self.path.startswith("/bundles/"):
            return self._send(404, {"error": "Bundle not found"})
        if self.path == "/nodes":
            return self._send(200, {"items": [{"id": "n1", "name": "laptop", "status": "online"}]})
        if self.path == "/executions/e1/tasks":
            s["polls"] += 1
            done = s["polls"] > 1
            tasks = [
                {"id": "t1", "seq": 0, "status": "completed" if done else "running", "exit_code": 0 if done else None,
                 "stdout": "loss 0.1", "node_id": "n1", "output_files": 1 if done else None},
                {"id": "t2", "seq": 1, "status": "failed" if done else "pending", "exit_code": 2 if done else None,
                 "stderr": "boom", "node_id": "n1"},
            ]
            status = "completed_with_errors" if done else "running"
            return self._send(200, {"batch": {"id": "e1", "status": status, "runtime": "project", "total": 2},
                                    "tasks": tasks})
        if self.path == "/executions/e1/tasks/t1/outputs":
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                info = tarfile.TarInfo("outputs/metrics.json")
                info.size = 2
                tar.addfile(info, io.BytesIO(b"{}"))
            return self._send(200, buf.getvalue(), "application/gzip")
        self._send(404, {"error": "nope"})


@pytest.fixture
def gateway(monkeypatch):
    FakeGateway.state = {"calls": [], "polls": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)
    monkeypatch.delenv("DC_TOKEN", raising=False)
    yield f"http://127.0.0.1:{server.server_address[1]}", FakeGateway.state
    server.shutdown()


def test_run_uploads_starts_follows_and_pulls(gateway, tmp_path, capsys, monkeypatch):
    url, state = gateway
    make_project(tmp_path)
    (tmp_path / "distributed.json").write_text(json.dumps({"project": {"command": "unused", "exclude": ["data/raw/"]}}))
    monkeypatch.chdir(tmp_path)
    code = cli.main(["--master", url, "run", "-C", str(tmp_path), "-i", "0.1", "-i", "0.01", "-d", "ds1:data/t.csv",
                     "--pull", "--", "python", "train.py", "--lr", "x"])
    assert code == 1, "a run with a failed task exits 1"
    req = state["request"]
    assert req["inputs"] == ["0.1", "0.01"]
    assert req["project"]["command"] == "python train.py --lr x"
    assert req["project"]["requirements"] == "requirements.txt"
    assert req["project"]["datasets"] == [{"datasetId": "ds1", "path": "data/t.csv"}]
    assert req["project"]["bundleId"] == hashlib.sha256(state["bundle"]).hexdigest()
    assert "data/raw/big.csv" not in members(state["bundle"])
    assert all(auth == "Bearer local-tok" for method, path, auth in state["calls"] if path != "/auth/local-token")
    out = capsys.readouterr()
    assert "━━ Task 1 · completed · exit 0" in out.out and "loss 0.1" in out.out
    assert "━━ Task 2 · failed · exit 2" in out.out and "boom" in out.out
    assert "dc pull e1" in out.err
    assert (tmp_path / "distributed-results" / "e1" / "task-001" / "outputs" / "metrics.json").read_text() == "{}"


def test_run_needs_a_command_and_reports_unreachable_masters(tmp_path, capsys):
    (tmp_path / "a.py").write_text("x")
    assert cli.main(["--master", "http://127.0.0.1:9", "run", "-C", str(tmp_path)]) == 2
    assert cli.main(["--master", "http://127.0.0.1:9", "nodes"]) == 1
    assert "Cannot reach the master" in capsys.readouterr().err
