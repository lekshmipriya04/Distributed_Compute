"""Which files of a folder go to the workers, and packing them.

The rules match the VS Code extension: version control, caches, virtual
environments and earlier outputs are always left out; in a git repository
.gitignore applies too; ``project.exclude`` in distributed.json adds more.
"""
from __future__ import annotations

import fnmatch
import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_EXCLUDES = (
    ".git/", ".hg/", ".svn/", "node_modules/", "__pycache__/", ".venv/", "venv/",
    ".mypy_cache/", ".pytest_cache/", ".ruff_cache/", ".ipynb_checkpoints/", ".idea/", ".vscode/",
    "distributed-results/", "outputs/", "*.pyc", ".DS_Store",
)
MAX_PROJECT_BYTES = 500 * 1024 * 1024
MAX_PROJECT_FILES = 50_000


def is_excluded(rel_path: str, patterns) -> bool:
    """gitignore-style, simplified (see the extension's isExcluded)."""
    parts = rel_path.replace("\\", "/").split("/")
    for raw in patterns:
        pattern = raw.strip()
        if pattern.startswith("./"):
            pattern = pattern[2:]
        if not pattern or pattern.startswith("#"):
            continue
        dir_only = pattern.endswith("/")
        body = pattern[:-1] if dir_only else pattern
        anchored = "/" in body  # "/x" or "a/b": matched from the project root
        body = body.lstrip("/")
        if anchored:
            limit = len(parts) - 1 if dir_only else len(parts)
            for i in range(1, limit + 1):
                if _match("/".join(parts[:i]), body):
                    return True
        else:
            names = parts[:-1] if dir_only else parts
            if any(fnmatch.fnmatchcase(name, body) for name in names):
                return True
    return False


def _match(path: str, glob: str) -> bool:
    # "**/" may match nothing; fnmatch's "*" crosses "/", which "**" needs anyway.
    return fnmatch.fnmatchcase(path, glob) or fnmatch.fnmatchcase(path, glob.replace("**/", ""))


def _git_files(folder: Path) -> Optional[list[str]]:
    try:
        out = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [f for f in out.decode("utf-8", "replace").split("\0") if f]


def _walk(folder: Path, excludes) -> list[str]:
    files: list[str] = []
    for root, dirs, names in os.walk(folder):
        rel_root = os.path.relpath(root, folder)
        rel_root = "" if rel_root == "." else rel_root.replace(os.sep, "/")
        dirs[:] = sorted(
            d for d in dirs
            if not os.path.islink(os.path.join(root, d))
            and not is_excluded(f"{rel_root}/{d}/x".lstrip("/"), excludes)
        )
        for name in names:
            rel = f"{rel_root}/{name}".lstrip("/")
            if not os.path.islink(os.path.join(root, name)) and not is_excluded(rel, excludes):
                files.append(rel)
                if len(files) > MAX_PROJECT_FILES:
                    raise ValueError(f"The folder has more than {MAX_PROJECT_FILES} files; "
                                     'add patterns to "project.exclude" in distributed.json')
    return files


@dataclass
class Packed:
    archive: bytes
    files: list[str]
    bytes: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.archive).hexdigest()


def pack(folder: Path, extra_excludes=()) -> Packed:
    """A .tar.gz of the folder. Sorted, no timestamps: same files, same hash."""
    excludes = list(DEFAULT_EXCLUDES) + list(extra_excludes)
    listed = _git_files(folder)
    files = sorted(f for f in (listed if listed is not None else _walk(folder, excludes))
                   if not is_excluded(f, excludes))
    if not files:
        raise ValueError("There are no files to send in this folder")
    buf = io.BytesIO()
    total = 0
    kept = []
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w",
                                                                           format=tarfile.USTAR_FORMAT) as tar:
        for rel in files:
            path = folder / rel
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            total += len(data)
            if total > MAX_PROJECT_BYTES:
                raise ValueError(f"The folder holds more than {MAX_PROJECT_BYTES // 2**20} MB. Upload large data "
                                 'as a dataset and add patterns to "project.exclude" in distributed.json')
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if (os.name != "nt" and os.access(path, os.X_OK)) or rel.endswith(".sh") else 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
            kept.append(rel)
    return Packed(buf.getvalue(), kept, total)


def read_project_block(folder: Path) -> dict:
    """The "project" block of distributed.json, or {}."""
    path = folder / "distributed.json"
    if not path.is_file():
        return {}
    try:
        block = json.loads(path.read_text("utf-8")).get("project") or {}
    except ValueError as exc:
        raise ValueError(f"distributed.json is not valid JSON: {exc}") from exc
    if not isinstance(block, dict):
        raise ValueError('"project" in distributed.json must be an object')
    return block


def safe_extract(archive: bytes, dest: Path) -> int:
    """Unpack an outputs archive into dest; refuses paths that leave it."""
    count = 0
    dest = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            target = (dest / member.name).resolve()
            if dest not in target.parents:
                raise ValueError(f"Unsafe path in the outputs archive: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, open(target, "wb") as out:
                out.write(src.read())
            count += 1
    return count
