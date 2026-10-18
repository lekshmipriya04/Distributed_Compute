"""Projects: run a whole folder of code on the workers, not just one file.

A project is a ``.tar.gz`` of the user's folder (a *bundle*), a shell command
to run in it, and optionally a requirements file, datasets to place in it and
folders to bring back afterwards. For each task the master

1. uploads the bundle (and any datasets) into the task's sandbox,
2. unpacks it with :data:`HELPER_SOURCE` (stdlib only, so it runs on any
   worker with ``python3``),
3. builds or reuses a Python environment from the requirements file,
4. runs the command inside the unpacked folder through the normal sandbox
   runner (same limits, timeout and cancel as any task),
5. packs the output folders and downloads them before the sandbox is deleted.

Bundles are stored once per owner and content hash, so an unchanged project is
never uploaded twice. Environments are built inside the sandbox (and deleted
with it) unless the node's owner lends an environment cache
(``env_cache_gb``); then they are kept in ``~/.cache/distributed-compute`` on
the worker, reused by every later task with the same requirements, and removed
when the node is removed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: File names inside a sandbox.
HELPER_FILENAME = "dc_project.py"
BUNDLE_FILENAME = "dc_bundle.tar.gz"
PROJECT_DIRNAME = "project"
OUTPUTS_ARCHIVE = "dc_outputs.tar.gz"

#: Where a worker keeps cached environments (only when the owner allows it).
WORKER_CACHE_DIR = ".cache/distributed-compute"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Relative paths inside a project: no absolute paths, no "..", no odd characters.
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9_.@+=,-]+(/[A-Za-z0-9_.@+=,-]+)*/?$")


class BundleError(ValueError):
    """The uploaded bundle is not a usable project archive."""


class BundleTooLarge(BundleError):
    """The archive, or what it unpacks to, is over a limit."""


def check_relative_path(path: str, what: str) -> str:
    """Normalise a path that must stay inside the project folder."""
    value = (path or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/")
    if not value or not _REL_PATH_RE.fullmatch(value):
        raise ValueError(f"{what} must be a relative path inside the project, such as outputs or data/train.csv")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"{what} must not contain '.' or '..'")
    return value


# --- bundles on the master -----------------------------------------------------

def _owner_dir(owner_id: str) -> str:
    # Owner ids are free-form; hash them so they are safe directory names.
    return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32]


def validate_bundle(path: str, *, max_unpacked_bytes: int, max_members: int) -> dict:
    """Check that ``path`` is a gzip tar that unpacks safely inside one folder.

    Returns ``{"files": n, "unpacked_bytes": b}``. Refuses absolute paths,
    ``..``, links, devices and archives that would unpack to more than the
    limits (a "zip bomb").
    """
    files = 0
    total = 0
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            for count, member in enumerate(tar, start=1):
                if count > max_members:
                    raise BundleTooLarge(f"The project has more than {max_members} files")
                name = member.name.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if name.startswith("/") or ".." in parts:
                    raise BundleError(f"Unsafe path in the project archive: {member.name}")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise BundleError(
                        f"The project archive may only hold files and folders; {member.name} is a link or device"
                    )
                files += 1
                total += member.size
                if total > max_unpacked_bytes:
                    raise BundleTooLarge(f"The project unpacks to more than {max_unpacked_bytes} bytes")
    except (tarfile.TarError, EOFError, OSError, gzip.BadGzipFile) as exc:
        raise BundleError(f"The project is not a valid .tar.gz archive: {exc}") from exc
    if files == 0:
        raise BundleError("The project archive is empty")
    return {"files": files, "unpacked_bytes": total}


class BundleStore:
    """Project bundles on the master, one file per owner and content hash."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def path(self, owner_id: str, sha256: str) -> Path:
        if not SHA256_RE.fullmatch(sha256 or ""):
            raise BundleError("A bundle id is the archive's sha256 (64 hex characters)")
        return self.root / _owner_dir(owner_id) / f"{sha256}.tar.gz"

    def exists(self, owner_id: str, sha256: str) -> bool:
        try:
            return self.path(owner_id, sha256).is_file()
        except BundleError:
            return False

    async def receive(
        self, owner_id: str, chunks, *, max_bytes: int, max_unpacked_bytes: int, max_members: int,
    ) -> dict:
        """Store an archive streamed as ``chunks``; returns its id and sizes.

        The file is written to a temporary name, checked, and only then moved
        into place under its hash, so a bad or partial upload leaves nothing.
        """
        import asyncio

        folder = self.root / _owner_dir(owner_id)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".upload-{secrets.token_hex(8)}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with open(tmp, "xb") as out:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise BundleTooLarge(f"The project archive is larger than the {max_bytes // 2**20} MB limit")
                    digest.update(chunk)
                    out.write(chunk)
            info = await asyncio.to_thread(
                validate_bundle, str(tmp), max_unpacked_bytes=max_unpacked_bytes, max_members=max_members,
            )
            sha = digest.hexdigest()
            os.replace(tmp, folder / f"{sha}.tar.gz")
        finally:
            tmp.unlink(missing_ok=True)
        return {"id": sha, "size_bytes": size, **info}


class OutputStore:
    """Output archives brought back from tasks, one per task."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.tar.gz"

    def delete(self, task_id: str) -> None:
        self.path(task_id).unlink(missing_ok=True)


# --- one project task ------------------------------------------------------------

@dataclass
class ProjectRun:
    """Everything the executor needs to run one project task on one node."""

    bundle_path: str                       # bundle on the master
    command: str
    output_path: str                       # where to store the outputs archive
    requirements: Optional[str] = None     # relative to the project
    outputs: list[str] = field(default_factory=lambda: ["outputs"])
    datasets: list[tuple[str, str]] = field(default_factory=list)  # (file on master, path in project)
    env_cache_bytes: int = 0               # 0 = build the environment in the sandbox
    env_timeout_seconds: int = 1800
    max_output_bytes: int = 500 * 1024 * 1024
    extra_env: dict[str, str] = field(default_factory=dict)


def project_env(sandbox: str, run: ProjectRun, *, task_index: int, task_count: int) -> dict[str, str]:
    """``DC_*`` variables every project command sees."""
    project = posixpath.join(sandbox, PROJECT_DIRNAME)
    first_output = run.outputs[0] if run.outputs else "outputs"
    env = {
        "DC_PROJECT_DIR": project,
        "DC_OUTPUT_DIR": posixpath.join(project, first_output),
        "DC_DATA_DIR": posixpath.join(project, "data"),
        "DC_INPUT_FILE": posixpath.join(sandbox, "input.txt"),
        "DC_TASK_INDEX": str(task_index),
        "DC_TASK_COUNT": str(task_count),
    }
    env.update(run.extra_env)
    return env


def parse_helper_json(stdout: str) -> Optional[dict]:
    """The helper's last ``DC_RESULT {...}`` line."""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("DC_RESULT "):
            try:
                value = json.loads(line[len("DC_RESULT "):])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def strip_results(text: str) -> str:
    """The helper's output without its internal DC_RESULT lines."""
    return "\n".join(line for line in (text or "").splitlines() if not line.startswith("DC_RESULT "))


def tail(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def remove_tree(path: str | os.PathLike[str]) -> None:
    shutil.rmtree(path, ignore_errors=True)


#: Worker-side helper. Uploaded into each project sandbox and run with python3.
#: Standard library only, Python 3.6+, POSIX.
HELPER_SOURCE = r'''"""Distributed Compute project helper (runs on the worker).

    python3 dc_project.py prepare SANDBOX DATA_JSON
    python3 dc_project.py env SANDBOX REQUIREMENTS CACHE_ROOT CACHE_BYTES TIMEOUT
    python3 dc_project.py pack SANDBOX MAX_BYTES OUTPUTS_JSON

Each prints one final line "DC_RESULT {json}".
"""
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time

PROJECT = "project"
BUNDLE = "dc_bundle.tar.gz"
ARCHIVE = "dc_outputs.tar.gz"
READY = ".dc-ready"


def result(**fields):
    sys.stdout.write("\nDC_RESULT " + json.dumps(fields) + "\n")
    sys.stdout.flush()


def fail(message, code=1):
    result(ok=False, error=message)
    sys.exit(code)


def inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def prepare(sandbox, data_json):
    project = os.path.join(sandbox, PROJECT)
    os.makedirs(project)
    bundle = os.path.join(sandbox, BUNDLE)
    files = 0
    with tarfile.open(bundle, "r:gz") as tar:
        for member in tar:
            name = member.name.replace("\\", "/")
            target = os.path.join(project, name)
            if name.startswith("/") or ".." in name.split("/") or not inside(target, project):
                fail("unsafe path in the project archive: %s" % member.name)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if not member.isfile():
                fail("the project archive may only hold files and folders: %s" % member.name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tar.extractfile(member)
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            files += 1
    os.unlink(bundle)
    placed = []
    for src_name, dest in json.loads(data_json or "[]"):
        src = os.path.join(sandbox, src_name)
        target = os.path.join(project, dest)
        if not inside(target, project) or not inside(src, sandbox):
            fail("a dataset path leaves the project: %s" % dest)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(src, target)
        placed.append(dest)
    os.makedirs(os.path.join(project, "outputs"), exist_ok=True)
    result(ok=True, files=files, datasets=placed)


def _dir_size(path):
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _evict(envs, keep, cache_bytes):
    """Delete least recently used environments until the cache fits."""
    entries = []
    for name in os.listdir(envs):
        path = os.path.join(envs, name)
        if name == keep or not os.path.isdir(path):
            continue
        ready = os.path.join(path, READY)
        used = os.path.getmtime(ready) if os.path.exists(ready) else 0
        entries.append((used, path))
    total = sum(_dir_size(p) for _u, p in entries) + _dir_size(os.path.join(envs, keep))
    for _used, path in sorted(entries):
        if total <= cache_bytes:
            break
        lock = open(path + ".lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            continue  # in use by another task
        size = _dir_size(path)
        shutil.rmtree(path, ignore_errors=True)
        total -= size
        lock.close()
        sys.stderr.write("[dc] removed an unused cached environment (%d MB)\n" % (size // 2**20))
    return total


def _run(cmd, env, deadline):
    left = deadline - time.time()
    if left <= 0:
        fail("the environment build timed out")
    sys.stderr.write("[dc] $ %s\n" % " ".join(cmd))
    sys.stderr.flush()
    try:
        proc = subprocess.run(cmd, env=env, stdout=sys.stderr, stderr=sys.stderr, timeout=left)
    except subprocess.TimeoutExpired:
        fail("the environment build timed out")
    return proc.returncode


def _build(env_dir, requirements, tool_env, deadline):
    uv = shutil.which("uv")
    if uv:
        if _run([uv, "venv", "--python", sys.executable, env_dir], tool_env, deadline) != 0:
            fail("uv could not create a virtual environment")
        rc = _run([uv, "pip", "install", "--python", os.path.join(env_dir, "bin", "python"),
                   "-r", requirements], tool_env, deadline)
        if rc != 0:
            fail("installing the requirements failed (uv pip install exited with %d)" % rc)
        return "uv"
    if _run([sys.executable, "-m", "venv", env_dir], tool_env, deadline) != 0:
        fail("python3 -m venv is not available on this worker. Install the python3-venv "
             "package (Debian/Ubuntu) or uv (https://docs.astral.sh/uv/) on it.")
    py = os.path.join(env_dir, "bin", "python")
    rc = _run([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
               "-r", requirements], tool_env, deadline)
    if rc != 0:
        fail("installing the requirements failed (pip install exited with %d)" % rc)
    return "pip"


def env(sandbox, requirements, cache_root, cache_bytes, timeout):
    started = time.time()
    deadline = started + float(timeout)
    cache_bytes = int(cache_bytes)
    project = os.path.join(sandbox, PROJECT)
    req = os.path.join(project, requirements)
    if not inside(req, project) or not os.path.isfile(req):
        fail("the requirements file %s is not in the project" % requirements)
    with open(req, "rb") as fh:
        content = fh.read()
    key = hashlib.sha256(b"\0".join([
        content, sys.version.encode(), platform.machine().encode(), platform.system().encode(),
    ])).hexdigest()[:20]
    tool_env = dict(os.environ)
    tool_env.pop("PYTHONPATH", None)
    tool_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    if cache_bytes <= 0:
        env_dir = os.path.join(sandbox, ".dc-env")
        tool_env["PIP_NO_CACHE_DIR"] = "1"
        tool_env["UV_CACHE_DIR"] = os.path.join(sandbox, ".dc-uv-cache")
        tool = _build(env_dir, req, tool_env, deadline)
        result(ok=True, path=env_dir, cached=False, reused=False, tool=tool,
               seconds=round(time.time() - started, 1))
        return
    envs = os.path.join(cache_root, "envs")
    os.makedirs(envs, exist_ok=True)
    tool_env["PIP_CACHE_DIR"] = os.path.join(cache_root, "pip")
    tool_env["UV_CACHE_DIR"] = os.path.join(cache_root, "uv")
    env_dir = os.path.join(envs, key)
    lock = open(env_dir + ".lock", "a")
    while True:  # another task on this node may be building the same environment
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > deadline:
                fail("timed out waiting for another task to build the environment")
            time.sleep(1)
    try:
        ready = os.path.join(env_dir, READY)
        if os.path.exists(ready):
            os.utime(ready, None)
            result(ok=True, path=env_dir, cached=True, reused=True, tool=None,
                   seconds=round(time.time() - started, 1))
            return
        shutil.rmtree(env_dir, ignore_errors=True)  # a build that did not finish
        try:
            tool = _build(env_dir, req, tool_env, deadline)
        except SystemExit:
            shutil.rmtree(env_dir, ignore_errors=True)
            raise
        with open(ready, "w") as fh:
            fh.write(requirements + "\n")
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    total = _evict(envs, key, cache_bytes)
    if total > cache_bytes:
        sys.stderr.write("[dc] the environment cache holds %d MB, more than the %d MB this node lends\n"
                         % (total // 2**20, cache_bytes // 2**20))
    result(ok=True, path=env_dir, cached=True, reused=False, tool=tool,
           seconds=round(time.time() - started, 1))


def pack(sandbox, max_bytes, outputs_json):
    project = os.path.join(sandbox, PROJECT)
    archive = os.path.join(sandbox, ARCHIVE)
    max_bytes = int(max_bytes)
    files = 0
    total = 0
    skipped = []
    with tarfile.open(archive, "w:gz") as tar:
        for rel in json.loads(outputs_json or "[]"):
            base = os.path.join(project, rel)
            if not inside(base, project) or not os.path.exists(base):
                continue
            if os.path.isfile(base) and not os.path.islink(base):
                candidates = [base]
            else:
                candidates = []
                for root, dirs, names in os.walk(base):
                    dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
                    candidates.extend(os.path.join(root, n) for n in sorted(names))
            for path in candidates:
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                name = os.path.relpath(path, project)
                if total + size > max_bytes:
                    skipped.append(name)
                    continue
                tar.add(path, arcname=name, recursive=False)
                files += 1
                total += size
    result(ok=True, files=files, bytes=total, skipped=skipped[:20], skipped_count=len(skipped))


def main():
    if len(sys.argv) < 3:
        fail("usage: dc_project.py prepare|env|pack SANDBOX ...", 2)
    op, args = sys.argv[1], sys.argv[2:]
    try:
        if op == "prepare":
            prepare(*args)
        elif op == "env":
            env(*args)
        elif op == "pack":
            pack(*args)
        else:
            fail("unknown operation %s" % op, 2)
    except SystemExit:
        raise
    except Exception as exc:
        fail("%s: %s" % (type(exc).__name__, exc))


if __name__ == "__main__":
    main()
'''
