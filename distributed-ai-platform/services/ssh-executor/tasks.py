"""Tasks domain module — batch lifecycle, distributed task execution, and sandboxing.

Unifies:
- Sandbox execution and OS adaptation (TaskExecutor, detect_os, select_adapter)
- Core pinning and multi-slot worker fan-out (CORE_POOL, make_spec, run_across_nodes)
- Batch orchestration and lease handling (acquire_allocations, lease_heartbeat, dispatch_batch)
- Batch & Task database persistence (BatchService, CANCELLABLE_STATUSES)
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import posixpath
import shlex
import shutil
import threading
import time
import types
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

import structlog
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from os_adapters.base import OSAdapter
from os_adapters.linux import LinuxAdapter
from os_adapters.macos import MacOSAdapter
from os_adapters.windows import WindowsAdapter
from runtimes import DEFAULT_EXECUTION_POLICY, ResourceSpec, policy_nice
from runtimes import CUSTOM_RUNTIME, get_runtime
import projects
from projects import ProjectRun
from schemas import BatchCreateRequest
from ssh import SSHTransport

logger = structlog.get_logger(__name__)

# ==============================================================================
# SECTION 1: Sandbox Constants & OS Adaptation
# ==============================================================================

SANDBOX_PREFIX = "dp-exec."
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
MACOS_EXTRA_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin"
INPUT_FILENAME = "input.txt"

CANCEL_POLL_SECONDS = 1.0
CAPACITY_POLL_SECONDS = 2.0
CAPACITY_WAIT_SECONDS = 300.0

CANCEL_STATUSES = ("cancelling", "cancelled")
CANCELLABLE_STATUSES = frozenset({"pending", "running"})
TERMINAL_BATCH_STATUSES = frozenset(
    {"cancelled", "completed", "completed_with_errors", "failed"}
)

OnUpdate = Callable[[int, str, dict], Awaitable[None]]
CancelCheck = Callable[[], Awaitable[bool]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _coerce_dt(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return fallback


def detect_os(transport: SSHTransport, timeout: int = 15) -> str:
    """Return ``'linux' | 'macos' | 'windows'`` for a connected transport."""
    res = transport.exec("uname -s", timeout=timeout)
    out = (res.get("stdout") or "").strip().lower()
    if res.get("exit_code") == 0 and out:
        return "macos" if "darwin" in out else "linux"
    res2 = transport.exec("echo %OS%", timeout=timeout)
    if "windows" in (res2.get("stdout") or "").strip().lower():
        return "windows"
    res3 = transport.exec(
        'powershell -NoProfile -Command "$PSVersionTable.PSVersion.Major"', timeout=timeout
    )
    if (res3.get("stdout") or "").strip():
        return "windows"
    raise RuntimeError("Could not determine the remote operating system")


def select_adapter(os_type: str) -> OSAdapter:
    return {
        "linux": LinuxAdapter,
        "macos": MacOSAdapter,
        "windows": WindowsAdapter,
    }.get((os_type or "").strip().lower(), LinuxAdapter)()


# ==============================================================================
# SECTION 2: TaskExecutor (Confinement & Sandboxing)
# ==============================================================================

class TaskExecutor:
    """Runs jobs in throwaway sandboxes over one :class:`SSHTransport`.

    The transport is opened lazily and left open when the caller already owns
    it (the dispatcher reuses one transport per worker slot).
    """

    def __init__(
        self,
        transport: SSHTransport,
        adapter: OSAdapter,
        *,
        nice: int = 19,
        max_output_bytes: int = 1_000_000,
        control_timeout: int = 30,
    ):
        self.transport = transport
        self.adapter = adapter
        self.nice = nice
        self.max_output_bytes = max_output_bytes
        self.control_timeout = control_timeout

    @property
    def _is_windows(self) -> bool:
        return self.adapter.os_type == "windows"

    def _run(self, command: str, *, timeout: Optional[int] = None) -> Dict[str, Any]:
        return self.transport.exec(command, timeout=timeout or self.control_timeout)

    def _path_for(self, node_path: Optional[str]) -> str:
        if node_path:
            return node_path
        if self._is_windows:
            return ""
        if self.adapter.os_type == "macos":
            return DEFAULT_PATH + ":" + MACOS_EXTRA_PATH
        return DEFAULT_PATH

    def _create_sandbox(self, execution_id: str) -> str:
        res = self._run(self.adapter.create_sandbox_command(execution_id))
        path = (res.get("stdout") or "").strip().splitlines()
        path = path[-1].strip() if path else ""
        if res.get("exit_code") != 0 or not path:
            raise RuntimeError(
                f"Could not create a sandbox on {self.transport.host}: "
                f"{(res.get('stderr') or '').strip()[:300]}"
            )
        return path

    def _remove_sandbox(self, sandbox: Optional[str]) -> None:
        if not sandbox:
            return
        try:
            self._run(
                self.adapter.remove_sandbox_command(sandbox),
                timeout=max(self.control_timeout, 30),
            )
        except Exception as exc:
            logger.warning("sandbox_cleanup_failed", host=self.transport.host,
                           sandbox=sandbox, error=str(exc))

    def _sandbox_path(self, sandbox: str, *parts: str) -> str:
        if self._is_windows:
            return "/".join((sandbox.rstrip("/\\"),) + tuple(parts))
        return posixpath.join(sandbox, *parts)

    def _write_file(self, path: str, content: str) -> None:
        self.transport.put_text(path, content)

    def cancel(self, execution_id: str) -> bool:
        if self._is_windows:
            res = self._run(self.adapter.cancel_command(execution_id))
            return res.get("exit_code") == 0
        res = self._run(self.adapter.cancel_command(execution_id))
        killed = bool((res.get("stdout") or "").strip())
        self._run(self.adapter.remove_sandbox_by_execution_command(execution_id))
        return killed

    def sweep_stale_sandboxes(self, older_than_minutes: int) -> int:
        try:
            res = self._run(
                self.adapter.sweep_sandboxes_command(older_than_minutes),
                timeout=max(self.control_timeout, 60),
            )
            out = (res.get("stdout") or "").strip()
            if not out:
                return 0
            return len([ln for ln in out.splitlines() if ln.strip()])
        except Exception as exc:
            logger.warning("sandbox_sweep_failed", host=self.transport.host, error=str(exc))
            return 0

    def run(
        self,
        runtime_id: str,
        code: str,
        input_data: str = "",
        spec: Optional[ResourceSpec] = None,
        *,
        env_path: Optional[str] = None,
        custom_command: Optional[str] = None,
        custom_filename: Optional[str] = None,
        execution_id: Optional[str] = None,
        extra_files: Optional[Dict[str, str]] = None,
        project: Optional[ProjectRun] = None,
        task_index: int = 0,
        task_count: int = 1,
    ) -> Dict[str, Any]:
        spec = spec or ResourceSpec(cpu_cores=1, memory_gb=1.0)
        execution_id = execution_id or f"exec_{uuid.uuid4().hex}"
        runtime = get_runtime(runtime_id)
        if runtime is None:
            return self._error_result(execution_id, f"Unknown runtime '{runtime_id}'")

        started = _now()
        sandbox: Optional[str] = None
        owns_connection = not self.transport.is_connected
        try:
            if owns_connection:
                self.transport.connect()
            sandbox = self._create_sandbox(execution_id)
            if project is not None:
                return self._run_project(
                    sandbox, runtime, project, input_data, spec,
                    env_path=env_path, execution_id=execution_id, started=started,
                    task_index=task_index, task_count=task_count,
                )
            return self._run_in_sandbox(
                sandbox, runtime, code, input_data, spec,
                env_path=env_path, custom_command=custom_command,
                custom_filename=custom_filename, execution_id=execution_id,
                extra_files=extra_files, started=started,
            )
        except Exception as exc:
            logger.error("task_execution_failed", host=self.transport.host,
                         runtime=runtime_id, error=str(exc))
            return {
                "status": "error",
                "exit_code": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "execution_id": execution_id,
                "started_at": _iso(started),
                "finished_at": _iso(_now()),
                "duration_ms": None,
            }
        finally:
            self._remove_sandbox(sandbox)
            if owns_connection:
                self.transport.close()

    def _run_in_sandbox(
        self,
        sandbox: str,
        runtime: Any,
        code: str,
        input_data: str,
        spec: ResourceSpec,
        *,
        env_path: Optional[str],
        custom_command: Optional[str],
        custom_filename: Optional[str],
        execution_id: str,
        extra_files: Optional[Dict[str, str]],
        started: datetime,
    ) -> Dict[str, Any]:
        filename = custom_filename if runtime.id == CUSTOM_RUNTIME and custom_filename else None
        source_name = runtime.source_filename(self.adapter.os_type) if filename is None else filename
        source_path = self._sandbox_path(sandbox, source_name)

        self._write_file(source_path, code)
        self._write_file(self._sandbox_path(sandbox, INPUT_FILENAME), input_data)
        for name, content in (extra_files or {}).items():
            self._write_file(self._sandbox_path(sandbox, name), content)
        for name, content in self.adapter.sandbox_support_files(runtime, spec).items():
            self._write_file(self._sandbox_path(sandbox, name), content)

        launch = self.adapter.build_sandbox_command(
            sandbox=sandbox,
            runtime=runtime,
            filename=source_name,
            spec=spec,
            nice=spec.nice,
            env_path=self._path_for(env_path),
            custom_command=custom_command,
            execution_id=execution_id,
            max_output_bytes=self.max_output_bytes,
        )
        res = self.transport.exec(
            launch,
            input_data=input_data,
            timeout=spec.timeout_seconds + 30,
            max_output_bytes=self.max_output_bytes,
        )
        finished = _now()
        stdout = res.get("stdout") or ""
        stderr = res.get("stderr") or ""
        exit_code = res.get("exit_code")
        if exit_code == 124 and "[task timed out]" not in stderr:
            stderr = (stderr + "\n[task timed out]").strip()
        status = "success" if exit_code == 0 else "error"
        duration_ms = int((finished - started).total_seconds() * 1000)
        return {
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "execution_id": execution_id,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "duration_ms": duration_ms,
            "runtime": runtime.id,
            "sandbox": sandbox,
        }

    def _helper(self, sandbox: str, *args: str, timeout: int) -> tuple[Dict[str, Any], dict]:
        """Run one step of the project helper; returns (raw result, its DC_RESULT)."""
        helper = self._sandbox_path(sandbox, projects.HELPER_FILENAME)
        command = " ".join(
            [shlex.quote(self.adapter.python_command()), shlex.quote(helper)] + list(args)
        )
        res = self.transport.exec(command, timeout=timeout, max_output_bytes=self.max_output_bytes)
        return res, projects.parse_helper_json(res.get("stdout") or "") or {}

    def _run_project(
        self,
        sandbox: str,
        runtime: Any,
        project: ProjectRun,
        input_data: str,
        spec: ResourceSpec,
        *,
        env_path: Optional[str],
        execution_id: str,
        started: datetime,
        task_index: int,
        task_count: int,
    ) -> Dict[str, Any]:
        """Unpack the project, build its environment, run its command, bring
        back its outputs. Setup failures are task failures (exit code set), not
        node failures, so they are not retried on other nodes."""
        notes: list[str] = []

        def failed(step: str, res: Dict[str, Any], info: dict) -> Dict[str, Any]:
            detail = info.get("error") or "see the log below"
            log = projects.tail(projects.strip_results((res.get("stderr") or "") + "\n" + (res.get("stdout") or "")))
            out = self._error_result(execution_id, f"[dc] {step} failed: {detail}\n{log}".strip())
            out.update(exit_code=res.get("exit_code") or 1, started_at=_iso(started),
                       duration_ms=int((_now() - started).total_seconds() * 1000))
            return out

        q = shlex.quote
        self._write_file(self._sandbox_path(sandbox, projects.HELPER_FILENAME), projects.HELPER_SOURCE)
        self._write_file(self._sandbox_path(sandbox, INPUT_FILENAME), input_data)
        for name, content in self.adapter.sandbox_support_files(runtime, spec).items():
            self._write_file(self._sandbox_path(sandbox, name), content)
        self.transport.upload(project.bundle_path, self._sandbox_path(sandbox, projects.BUNDLE_FILENAME))
        placements = []
        for i, (local, dest) in enumerate(project.datasets):
            name = f"dc_data_{i}"
            self.transport.upload(local, self._sandbox_path(sandbox, name))
            placements.append([name, dest])
        res, info = self._helper(sandbox, "prepare", q(sandbox), q(json.dumps(placements)), timeout=600)
        if res.get("exit_code") != 0 or not info.get("ok"):
            return failed("Unpacking the project", res, info)

        task_path = self._path_for(env_path)
        extra_env = projects.project_env(sandbox, project, task_index=task_index, task_count=task_count)
        if project.requirements:
            cache_root = '"$HOME"/' + projects.WORKER_CACHE_DIR
            res, info = self._helper(
                sandbox, "env", q(sandbox), q(project.requirements), cache_root,
                str(int(project.env_cache_bytes)), str(int(project.env_timeout_seconds)),
                timeout=project.env_timeout_seconds + 60,
            )
            if res.get("exit_code") != 0 or not info.get("ok"):
                return failed("Building the environment", res, info)
            env_dir = str(info["path"])
            extra_env["VIRTUAL_ENV"] = env_dir
            task_path = f"{env_dir}/bin:{task_path}" if task_path else f"{env_dir}/bin"
            if info.get("reused"):
                notes.append(f"[dc] environment: reused the cached one ({info.get('seconds')} s)")
            elif info.get("cached"):
                notes.append(f"[dc] environment: built with {info.get('tool')} in {info.get('seconds')} s, "
                             "cached on this node for later tasks")
            else:
                notes.append(f"[dc] environment: built with {info.get('tool')} in {info.get('seconds')} s; "
                             "it is deleted after the task (lend an environment cache on this node to reuse it)")

        command = f"cd {projects.PROJECT_DIRNAME} && {project.command}"
        launch = self.adapter.build_sandbox_command(
            sandbox=sandbox,
            runtime=runtime,
            filename=projects.HELPER_FILENAME,
            spec=spec,
            nice=spec.nice,
            env_path=task_path,
            custom_command=None,
            execution_id=execution_id,
            max_output_bytes=self.max_output_bytes,
            command_override=command,
            extra_env=extra_env,
        )
        res = self.transport.exec(
            launch, input_data=input_data, timeout=spec.timeout_seconds + 30,
            max_output_bytes=self.max_output_bytes,
        )
        exit_code = res.get("exit_code")
        stdout = res.get("stdout") or ""
        stderr = res.get("stderr") or ""
        if exit_code == 124 and "[task timed out]" not in stderr:
            stderr = (stderr + "\n[task timed out]").strip()

        output_files = output_bytes = None
        if project.outputs:
            pres, pinfo = self._helper(
                sandbox, "pack", q(sandbox), str(int(project.max_output_bytes)),
                q(json.dumps(project.outputs)), timeout=600,
            )
            if pres.get("exit_code") == 0 and pinfo.get("ok"):
                output_files = int(pinfo.get("files") or 0)
                output_bytes = int(pinfo.get("bytes") or 0)
                if output_files:
                    self.transport.fetch(
                        self._sandbox_path(sandbox, projects.OUTPUTS_ARCHIVE), project.output_path
                    )
                if pinfo.get("skipped_count"):
                    notes.append(
                        f"[dc] outputs: {pinfo['skipped_count']} file(s) left out, over the "
                        f"{project.max_output_bytes // 2**20} MB limit: " + ", ".join(pinfo.get("skipped") or [])
                    )
            else:
                notes.append("[dc] outputs could not be collected: "
                             + (pinfo.get("error") or projects.tail(pres.get("stderr") or "", 500)))
        if notes:
            stderr = "\n".join(notes) + ("\n" + stderr if stderr else "")
        finished = _now()
        return {
            "status": "success" if exit_code == 0 else "error",
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "execution_id": execution_id,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "duration_ms": int((finished - started).total_seconds() * 1000),
            "runtime": runtime.id,
            "sandbox": sandbox,
            "output_files": output_files,
            "output_bytes": output_bytes,
        }

    def run_python(
        self,
        code: str,
        input_data: str = "",
        spec: Optional[ResourceSpec] = None,
        execution_id: Optional[str] = None,
        strict: bool = False,
        env_path: Optional[str] = None,
        extra_files: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return self.run(
            "python", code, input_data, spec,
            env_path=env_path, execution_id=execution_id, extra_files=extra_files,
        )

    def _error_result(self, execution_id: str, message: str) -> Dict[str, Any]:
        now = _now()
        return {
            "status": "error",
            "exit_code": None,
            "stdout": "",
            "stderr": message,
            "execution_id": execution_id,
            "started_at": _iso(now),
            "finished_at": _iso(now),
            "duration_ms": 0,
        }


# ==============================================================================
# SECTION 3: Resource Pooling & Slot Fan-Out
# ==============================================================================

import leases as lease_logic
from leases import (
    NodeAllocation,
    node_gpu_enforceable,
    release_allocations,
    renew_allocations,
    reserve_allocations,
    select_runtime_nodes,
    track_live_job,
)


class _CorePool:
    def __init__(self) -> None:
        self._busy: dict[str, set[int]] = {}
        self._lock = threading.Lock()

    def acquire(self, node_id: Any, total: int, count: int) -> Optional[list[int]]:
        count = max(1, int(count))
        total = max(int(total or 0), count)
        with self._lock:
            busy = self._busy.setdefault(str(node_id), set())
            for start in range(0, total - count + 1, count):
                block = list(range(start, start + count))
                if not busy.intersection(block):
                    busy.update(block)
                    return block
            free = [c for c in range(total) if c not in busy]
            if len(free) < count:
                return None
            chosen = free[:count]
            busy.update(chosen)
            return chosen

    def release(self, node_id: Any, cores: list[int]) -> None:
        with self._lock:
            busy = self._busy.get(str(node_id))
            if busy is not None:
                busy.difference_update(cores)
                if not busy:
                    self._busy.pop(str(node_id), None)

    def busy(self, node_id: Any) -> set[int]:
        with self._lock:
            return set(self._busy.get(str(node_id), ()))


CORE_POOL = _CorePool()


def make_spec(
    cores: list[int],
    memory_gb: float,
    timeout_seconds: int,
    *,
    nice: int = 19,
    gpu_count: int = 0,
    gpu_memory_fraction: float = 0.0,
    gpu_memory_gb: float = 0.0,
    gpu_enforceable: bool = False,
) -> ResourceSpec:
    fraction = max(0.0, min(1.0, float(gpu_memory_fraction or 0.0)))
    kwargs: dict[str, Any] = {
        "cpu_cores": max(1, len(cores)),
        "memory_gb": max(0.0, float(memory_gb or 0.0)),
        "timeout_seconds": max(1, int(timeout_seconds)),
        "nice": max(0, min(19, int(nice))),
        "gpu_count": max(0, int(gpu_count or 0)) if fraction > 0 else 0,
        "gpu_memory_fraction": fraction if gpu_count else 0.0,
        "gpu_memory_gb": max(0.0, float(gpu_memory_gb or 0.0)) if fraction > 0 else 0.0,
        "gpu_enforceable": bool(gpu_enforceable),
    }
    fields = {f.name: f for f in dataclasses.fields(ResourceSpec)}
    for name in ("cores", "core_ids", "cpu_ids", "cpu_list", "cpu_set", "cpuset", "core_list"):
        if name in fields:
            kind = str(fields[name].type)
            kwargs[name] = (
                ",".join(str(c) for c in cores)
                if "str" in kind and "tuple" not in kind and "list" not in kind
                else tuple(cores)
            )
            break
    else:
        for name in ("core_offset", "cpu_offset", "first_core", "core_start"):
            if name in fields:
                kwargs[name] = int(cores[0]) if cores else 0
                break
    return ResourceSpec(**kwargs)


@dataclass
class _Slot:
    alloc: NodeAllocation
    index: int
    cores: list[int]
    pooled: bool
    transport: Any = None
    executor: Any = None

    @property
    def node(self):
        return self.alloc.node

    @property
    def node_id(self):
        return self.alloc.node.id


class _Sweeper:
    def __init__(self, minutes: Optional[int]) -> None:
        self._minutes = minutes
        self._done: set[str] = set()
        self._lock = threading.Lock()

    def maybe_sweep(self, node_id: Any, executor: Any) -> None:
        if not self._minutes:
            return
        with self._lock:
            if str(node_id) in self._done:
                return
            self._done.add(str(node_id))
        try:
            executor.sweep_stale_sandboxes(self._minutes)
        except Exception as exc:
            logger.warning("sandbox_sweep_failed", node_id=str(node_id), error=str(exc))


def _open_slot(slot: _Slot, nice: int, max_output_bytes: int, sweeper: _Sweeper) -> Any:
    if slot.executor is None:
        from node_service import transport_for_node
        transport = transport_for_node(slot.node)
        transport.connect()
        slot.transport = transport
        slot.executor = TaskExecutor(
            transport,
            select_adapter(getattr(slot.node, "os_type", None) or "linux"),
            nice=nice,
            max_output_bytes=max_output_bytes,
        )
        sweeper.maybe_sweep(slot.node_id, slot.executor)
    return slot.executor


def _close_slot(slot: _Slot) -> None:
    transport, slot.transport, slot.executor = slot.transport, None, None
    if transport is not None:
        try:
            transport.close()
        except Exception:
            pass


def _cancel_in_slot(slot: _Slot, execution_id: str) -> None:
    executor = slot.executor
    if executor is None:
        return
    try:
        executor.cancel(execution_id)
    except Exception as exc:
        logger.warning("task_cancel_failed", node_id=str(slot.node_id), error=str(exc))


def classify_result(result: dict) -> str:
    status = str(result.get("status") or "").lower()
    exit_code = result.get("exit_code")
    if status in ("success", "completed"):
        return "completed"
    if status == "cancelled":
        return "cancelled"
    if status == "timeout":
        return "failed"
    if exit_code is None:
        return "infra"
    if exit_code == 0 and status not in ("error", "failed"):
        return "completed"
    return "failed"


@dataclass
class _Item:
    idx: int
    attempts: int = 0
    tried: set = field(default_factory=set)


async def run_across_nodes(
    allocations: list[NodeAllocation],
    code: str,
    inputs: list[str],
    *,
    timeout_seconds: int,
    nice: int,
    max_output_bytes: int,
    runtime_id: str = "python",
    custom_command: Optional[str] = None,
    custom_filename: Optional[str] = None,
    files_for: Optional[Callable[[int], dict[str, str]]] = None,
    on_update: Optional[OnUpdate] = None,
    cancel_check: Optional[CancelCheck] = None,
    sweep_minutes: Optional[int] = None,
    affinity: Optional[list] = None,
    project_for: Optional[Callable[[int, Any], ProjectRun]] = None,
) -> list[dict]:
    n = len(inputs)
    if n == 0:
        return []
    results: list[Optional[dict]] = [None] * n
    if not allocations:
        now = _now()
        return [
            _terminal(i, "failed", {"stderr": "No node with a resource lease is available"},
                      None, 0, None, now, now, infra=True)
            for i in range(n)
        ]

    node_ids: list = []
    for alloc in allocations:
        if alloc.node.id not in node_ids:
            node_ids.append(alloc.node.id)
    max_attempts = max(1, min(len(node_ids), 3))

    pending: deque[_Item] = deque(_Item(i) for i in range(n))
    running: dict[int, tuple[_Slot, str]] = {}
    dead: set = set()
    state = {"cancelled": False, "settled": 0}
    cond = asyncio.Condition()
    sweeper = _Sweeper(sweep_minutes)

    slots: list[_Slot] = []
    for alloc in allocations:
        total = int(getattr(alloc.node, "allocated_cpu", 0) or alloc.cpu_allocated)
        for i in range(alloc.slots):
            cores = CORE_POOL.acquire(alloc.node.id, total, alloc.cpu_per_task)
            pooled = cores is not None
            slots.append(
                _Slot(alloc, i, cores if pooled else lease_logic.core_slice(i, alloc.cpu_per_task), pooled)
            )

    async def notify(idx: int, phase: str, info: dict) -> None:
        if on_update is None:
            return
        try:
            await on_update(idx, phase, info)
        except Exception as exc:
            logger.error("dispatch_on_update_failed", idx=idx, phase=phase, error=str(exc))

    def settle_locked(idx: int, info: dict) -> None:
        if results[idx] is None:
            results[idx] = info
            state["settled"] += 1

    def take_locked(node_id) -> Optional[_Item]:
        for item in pending:
            if node_id in item.tried:
                continue
            pref = affinity[item.idx] if affinity else None
            if pref is None or pref == node_id or pref in dead or pref in item.tried:
                pending.remove(item)
                return item
        return None

    def strand_check_locked() -> list[dict]:
        stranded = []
        live = [nid for nid in node_ids if nid not in dead]
        for item in list(pending):
            if not any(nid not in item.tried for nid in live):
                pending.remove(item)
                now = _now()
                info = _terminal(
                    item.idx, "failed",
                    {"stderr": "No reachable node left to run this task"},
                    None, item.attempts, None, now, now, infra=True,
                )
                settle_locked(item.idx, info)
                stranded.append(info)
        return stranded

    async def worker(slot: _Slot) -> None:
        node_id = slot.node_id
        alloc = slot.alloc
        spec = make_spec(
            slot.cores, alloc.memory_per_slot_gb, timeout_seconds,
            nice=nice,
            gpu_count=getattr(alloc, "gpu_allocated", 0),
            gpu_memory_fraction=getattr(alloc, "gpu_fraction_per_slot", 0.0),
            gpu_memory_gb=getattr(alloc, "gpu_memory_per_slot_gb", 0.0),
            gpu_enforceable=node_gpu_enforceable(alloc.node),
        )
        try:
            while True:
                async with cond:
                    while True:
                        if state["cancelled"] or node_id in dead or state["settled"] >= n:
                            return
                        item = take_locked(node_id)
                        if item is not None:
                            break
                        await cond.wait()
                    item.attempts += 1
                    item.tried.add(node_id)
                    execution_id = f"exec_{uuid.uuid4().hex}"
                    running[item.idx] = (slot, execution_id)

                started = _now()
                await notify(item.idx, "running", {
                    "node_id": node_id, "attempts": item.attempts,
                    "execution_id": execution_id, "started_at": started,
                })
                extra = files_for(item.idx) if files_for else None
                try:
                    project = project_for(item.idx, alloc.node) if project_for else None
                    raw = await asyncio.to_thread(
                        _run_in_slot, slot, sweeper,
                        runtime_id=runtime_id, code=code, input_data=inputs[item.idx] or "",
                        spec=spec, custom_command=custom_command,
                        custom_filename=custom_filename, execution_id=execution_id,
                        extra_files=extra, nice=nice, max_output_bytes=max_output_bytes,
                        project=project, task_index=item.idx, task_count=n,
                    )
                    outcome = classify_result(raw or {})
                except Exception as exc:
                    raw = {"status": "error", "exit_code": None, "stdout": "",
                           "stderr": f"{type(exc).__name__}: {exc}"}
                    outcome = "infra"
                if outcome == "infra":
                    await asyncio.to_thread(_close_slot, slot)
                finished = _now()

                to_notify: list[tuple[int, str, dict]] = []
                requeue = False
                async with cond:
                    running.pop(item.idx, None)
                    if state["cancelled"] and outcome != "completed":
                        outcome = "cancelled"
                    if outcome == "infra":
                        dead.add(node_id)
                        logger.warning(
                            "dispatch_node_lost", node_id=str(node_id),
                            error=str(raw.get("stderr"))[:300],
                        )
                        live_untried = [
                            nid for nid in node_ids if nid not in dead and nid not in item.tried
                        ]
                        if item.attempts < max_attempts and live_untried:
                            requeue = True
                        else:
                            info = _terminal(item.idx, "failed", raw, node_id, item.attempts,
                                             execution_id, started, finished, infra=True)
                            settle_locked(item.idx, info)
                            to_notify.append((item.idx, "failed", info))
                        for info in strand_check_locked():
                            to_notify.append((info["idx"], "failed", info))
                    else:
                        info = _terminal(item.idx, outcome, raw, node_id, item.attempts,
                                         execution_id, started, finished, infra=False)
                        settle_locked(item.idx, info)
                        to_notify.append((item.idx, outcome, info))
                    cond.notify_all()

                if requeue:
                    await notify(item.idx, "retrying", {
                        "node_id": node_id, "attempts": item.attempts,
                        "stderr": f"Attempt {item.attempts} on node {node_id} failed "
                                  f"({str(raw.get('stderr') or '').strip()[:200]}); retrying on another node",
                    })
                    async with cond:
                        if state["cancelled"]:
                            now = _now()
                            info = _terminal(item.idx, "cancelled", {}, None, item.attempts,
                                             None, now, now, infra=False)
                            settle_locked(item.idx, info)
                            to_notify.append((item.idx, "cancelled", info))
                        else:
                            pending.appendleft(item)
                            for info in strand_check_locked():
                                to_notify.append((info["idx"], "failed", info))
                        cond.notify_all()
                for idx, phase, info in to_notify:
                    await notify(idx, phase, info)
        finally:
            await asyncio.to_thread(_close_slot, slot)

    async def watch_cancel() -> None:
        while True:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            try:
                requested = await cancel_check()
            except Exception as exc:
                logger.warning("cancel_check_failed", error=str(exc))
                continue
            if not requested:
                continue
            to_notify = []
            async with cond:
                state["cancelled"] = True
                to_kill = list(running.values())
                while pending:
                    item = pending.popleft()
                    now = _now()
                    info = _terminal(item.idx, "cancelled", {}, None, item.attempts,
                                     None, now, now, infra=False)
                    settle_locked(item.idx, info)
                    to_notify.append(info)
                cond.notify_all()
            logger.info("dispatch_cancelling", running=len(to_kill), dropped=len(to_notify))
            for slot, execution_id in to_kill:
                await asyncio.to_thread(_cancel_in_slot, slot, execution_id)
            for info in to_notify:
                await notify(info["idx"], "cancelled", info)
            return

    watcher = asyncio.create_task(watch_cancel()) if cancel_check else None
    try:
        await asyncio.gather(*(worker(s) for s in slots))
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        for s in slots:
            if s.pooled:
                CORE_POOL.release(s.node_id, s.cores)

    for idx in range(n):
        if results[idx] is None:
            now = _now()
            phase = "cancelled" if state["cancelled"] else "failed"
            info = _terminal(idx, phase, {"stderr": "" if state["cancelled"] else
                                          "No reachable node left to run this task"},
                             None, 0, None, now, now, infra=not state["cancelled"])
            results[idx] = info
            await notify(idx, phase, info)
    return results


def _run_in_slot(slot: _Slot, sweeper: _Sweeper, *, runtime_id, code, input_data, spec,
                 custom_command, custom_filename, execution_id, extra_files,
                 nice, max_output_bytes, project=None, task_index=0, task_count=1) -> dict:
    executor = _open_slot(slot, nice, max_output_bytes, sweeper)
    return executor.run(
        runtime_id,
        code,
        input_data,
        spec,
        env_path=getattr(slot.node, "env_path", None),
        custom_command=custom_command,
        custom_filename=custom_filename,
        execution_id=execution_id,
        extra_files=extra_files,
        project=project,
        task_index=task_index,
        task_count=task_count,
    )


def _terminal(idx: int, status: str, raw: dict, node_id, attempts: int,
              execution_id: Optional[str], started: datetime, finished: datetime,
              *, infra: bool) -> dict:
    started_at = _coerce_dt(raw.get("started_at"), started)
    finished_at = _coerce_dt(raw.get("finished_at"), finished)
    duration = raw.get("duration_ms")
    if not isinstance(duration, int):
        duration = max(0, int((finished_at - started_at).total_seconds() * 1000))
    return {
        "idx": idx,
        "status": status,
        "exit_code": raw.get("exit_code"),
        "stdout": raw.get("stdout") or "",
        "stderr": raw.get("stderr") or "",
        "node_id": node_id,
        "attempts": attempts,
        "execution_id": raw.get("execution_id") or execution_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration,
        "infra_error": infra,
        "output_files": raw.get("output_files"),
        "output_bytes": raw.get("output_bytes"),
    }


# ==============================================================================
# SECTION 4: Lease Acquisition & Heartbeat
# ==============================================================================

async def acquire_allocations(
    owner_id: str,
    nodes: list,
    *,
    job_id: str,
    cpu_per_task: int = 1,
    max_tasks: Optional[int] = None,
    ttl_seconds: int = 3600,
    cancel_check: Optional[CancelCheck] = None,
    on_wait: Optional[Callable[[], Awaitable[None]]] = None,
    wait_seconds: Optional[float] = None,
) -> list[NodeAllocation]:
    budget = CAPACITY_WAIT_SECONDS if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + max(0.0, budget)
    waited = False
    while True:
        allocations = await reserve_allocations(
            owner_id, nodes, job_id=job_id, cpu_per_task=cpu_per_task,
            max_tasks=max_tasks, ttl_seconds=ttl_seconds,
        )
        if allocations:
            return allocations
        if time.monotonic() >= deadline:
            return []
        if cancel_check is not None and await cancel_check():
            return []
        if on_wait is not None and not waited:
            await on_wait()
        waited = True
        await asyncio.sleep(CAPACITY_POLL_SECONDS)


@contextlib.asynccontextmanager
async def lease_heartbeat(
    owner_id: str, allocations: list[NodeAllocation], ttl_seconds: int
) -> AsyncIterator[None]:
    interval = max(5.0, float(ttl_seconds) / 3.0)

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            await renew_allocations(owner_id, allocations, ttl_seconds)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ==============================================================================
# SECTION 5: Batch Dispatching
# ==============================================================================

from config import get_settings
from database import get_db_manager
from models import SSHNode, Task, TaskBatch


async def _load_batch(batch_id: uuid.UUID) -> Optional[tuple[Any, list[Any]]]:
    async with get_db_manager().session() as session:
        batch = (
            await session.execute(select(TaskBatch).where(TaskBatch.id == batch_id))
        ).scalar_one_or_none()
        if not batch:
            return None
        tasks = list(
            (
                await session.execute(
                    select(Task).where(Task.batch_id == batch_id).order_by(Task.seq)
                )
            )
            .scalars()
            .all()
        )
        snap = types.SimpleNamespace(
            id=batch.id, owner_id=batch.owner_id, code=batch.code,
            runtime=batch.runtime or "python", custom_command=batch.custom_command,
            custom_filename=batch.custom_filename, cpu_per_task=batch.cpu_per_task or 1,
            timeout_seconds=batch.timeout_seconds, node_ids_json=batch.node_ids_json,
            status=batch.status,
            execution_policy=getattr(batch, "execution_policy", None) or DEFAULT_EXECUTION_POLICY,
            project_json=batch.project_json,
        )
        task_snaps = [
            types.SimpleNamespace(id=t.id, seq=t.seq, input_data=t.input_data or "")
            for t in tasks
        ]
        return snap, task_snaps


async def _select_nodes(owner_id: str, node_ids: Optional[list]) -> list[SSHNode]:
    async with get_db_manager().session() as session:
        stmt = select(SSHNode).where(SSHNode.owner_id == owner_id, SSHNode.status == "online")
        if node_ids:
            stmt = stmt.where(SSHNode.id.in_(list(node_ids)))
        return list((await session.execute(stmt)).scalars().all())


async def _update_task(task_id: uuid.UUID, **fields) -> None:
    async with get_db_manager().session() as session:
        task = (
            await session.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if not task:
            return
        for key, value in fields.items():
            setattr(task, key, value)
        session.add(task)


async def _batch_status(batch_id: uuid.UUID) -> Optional[str]:
    async with get_db_manager().session() as session:
        return (
            await session.execute(select(TaskBatch.status).where(TaskBatch.id == batch_id))
        ).scalar_one_or_none()


async def _transition_batch(batch_id: uuid.UUID, from_statuses: tuple, **fields) -> bool:
    async with get_db_manager().session() as session:
        res = await session.execute(
            update(TaskBatch)
            .where(TaskBatch.id == batch_id, TaskBatch.status.in_(from_statuses))
            .values(**fields)
        )
        return bool(res.rowcount)


async def _finalize_batch(batch_id: uuid.UUID, failure: Optional[str] = None) -> Optional[str]:
    now = _now()
    async with get_db_manager().session() as session:
        batch = (
            await session.execute(select(TaskBatch).where(TaskBatch.id == batch_id))
        ).scalar_one_or_none()
        if not batch:
            return None
        tasks = list(
            (await session.execute(select(Task).where(Task.batch_id == batch_id)))
            .scalars()
            .all()
        )
        cancelling = batch.status in CANCEL_STATUSES
        for task in tasks:
            if task.status in ("pending", "running"):
                if cancelling:
                    task.status = "cancelled"
                else:
                    task.status = "failed"
                    task.stderr = task.stderr or failure or "Task was not executed"
                task.finished_at = task.finished_at or now
        batch.completed = sum(1 for t in tasks if t.status == "completed")
        batch.failed = sum(1 for t in tasks if t.status == "failed")
        batch.cancelled = sum(1 for t in tasks if t.status == "cancelled")
        if cancelling:
            batch.status = "cancelled"
            batch.message = "Cancelled by user"
        elif failure:
            batch.status = "failed"
            batch.message = failure
        elif batch.failed == 0:
            batch.status = "completed"
            batch.message = None
        else:
            batch.status = "completed_with_errors"
            batch.message = f"{batch.failed} of {len(tasks)} task(s) failed"
        batch.finished_at = now
        session.add(batch)
        return batch.status


def _parse_node_ids(raw: Optional[str]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    try:
        for value in json.loads(raw or "[]") or []:
            try:
                out.append(uuid.UUID(str(value)))
            except (TypeError, ValueError):
                continue
    except (TypeError, ValueError):
        pass
    return out


def _task_fields(phase: str, info: dict) -> dict:
    if phase == "running":
        return {
            "status": "running", "node_id": info["node_id"], "attempts": info["attempts"],
            "started_at": info["started_at"], "finished_at": None, "duration_ms": None,
        }
    if phase == "retrying":
        return {"status": "pending", "attempts": info["attempts"], "stderr": info.get("stderr")}
    return {
        "status": phase,
        "node_id": info.get("node_id"),
        "exit_code": info.get("exit_code"),
        "stdout": info.get("stdout"),
        "stderr": info.get("stderr"),
        "attempts": info.get("attempts", 0),
        "started_at": info.get("started_at"),
        "finished_at": info.get("finished_at"),
        "duration_ms": info.get("duration_ms"),
        "output_files": info.get("output_files"),
        "output_bytes": info.get("output_bytes"),
    }


PROJECT_RUNTIME = "project"


class ProjectSetupError(RuntimeError):
    """A project batch cannot start (bundle gone, dataset unreadable)."""


def project_nodes(nodes: list) -> list:
    """Online POSIX nodes with python3: projects need the helper to run."""
    return [n for n in select_runtime_nodes(nodes, "python") if (getattr(n, "os_type", None) or "linux") != "windows"]


def project_stores(settings) -> tuple[projects.BundleStore, projects.OutputStore, str]:
    root = settings.project_data_dir
    return (
        projects.BundleStore(posixpath.join(root, "bundles")),
        projects.OutputStore(posixpath.join(root, "outputs")),
        posixpath.join(root, "datasets"),
    )


def _dataset_filename(metadata: dict) -> str:
    fmt = (metadata.get("file_format") or "csv").lower()
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (metadata.get("name") or "dataset"))
    stem = stem.strip("_") or "dataset"
    return stem if stem.lower().endswith("." + fmt) else f"{stem}.{fmt}"


async def _fetch_dataset_copy(dataset_id: str, owner_id: str, cache_dir: str, settings) -> tuple[str, dict]:
    """A local copy of an owner's dataset (kept and reused while its size matches)."""
    from training import _download_to_file, _fetch_metadata

    try:
        metadata = await _fetch_metadata(dataset_id, settings, owner_id)
    except Exception as exc:
        raise ProjectSetupError(f"Dataset {dataset_id}: {exc}") from exc
    folder = posixpath.join(cache_dir, projects._owner_dir(owner_id))
    os.makedirs(folder, exist_ok=True)
    target = posixpath.join(folder, f"{metadata['id']}.{(metadata.get('file_format') or 'csv').lower()}")
    expected = metadata.get("size_bytes")
    if not (os.path.isfile(target) and (expected is None or os.path.getsize(target) == expected)):
        try:
            tmp = await asyncio.to_thread(_download_to_file, metadata, settings, owner_id)
        except Exception as exc:
            raise ProjectSetupError(f"Dataset {dataset_id}: {exc}") from exc
        shutil.move(tmp, target)
    return target, metadata


async def _prepare_project(batch, tasks: list, settings) -> Callable[[int, Any], ProjectRun]:
    """Check the bundle, fetch the datasets, and return the per-task runs."""
    from schemas import ProjectSpec

    spec = ProjectSpec.model_validate_json(batch.project_json or "{}")
    bundles, outputs, dataset_dir = project_stores(settings)
    bundle = bundles.path(batch.owner_id, spec.bundle_id)
    if not bundle.is_file():
        raise ProjectSetupError("The project files are no longer on the master; run the project again to upload them")
    datasets: list[tuple[str, str]] = []
    for item in spec.datasets:
        local, metadata = await _fetch_dataset_copy(str(item.dataset_id), batch.owner_id, dataset_dir, settings)
        datasets.append((local, item.path or f"data/{_dataset_filename(metadata)}"))
    outputs.root.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        outputs.delete(str(task.id))

    def project_for(idx: int, node: Any) -> ProjectRun:
        return ProjectRun(
            bundle_path=str(bundle),
            command=spec.command,
            output_path=str(outputs.path(str(tasks[idx].id))),
            requirements=spec.requirements,
            outputs=list(spec.outputs),
            datasets=datasets,
            env_cache_bytes=int(float(getattr(node, "env_cache_gb", 0.0) or 0.0) * 1024 ** 3),
            env_timeout_seconds=settings.env_build_timeout_seconds,
            max_output_bytes=settings.max_project_output_bytes,
        )

    return project_for


async def dispatch_batch(batch_id: uuid.UUID, node_ids: Optional[list] = None) -> None:
    settings = get_settings()
    with track_live_job(batch_id):
        loaded = await _load_batch(batch_id)
        if loaded is None:
            logger.error("dispatch_batch_missing", batch_id=str(batch_id))
            return
        batch, tasks = loaded
        if batch.status in CANCEL_STATUSES:
            await _finalize_batch(batch_id)
            return
        if batch.status != "pending":
            logger.warning("dispatch_batch_not_pending", batch_id=str(batch_id), status=batch.status)
            return

        owner_id = batch.owner_id
        if node_ids is None:
            node_ids = _parse_node_ids(batch.node_ids_json)
        cpu_per_task = max(1, int(batch.cpu_per_task or 1))
        timeout = int(batch.timeout_seconds or settings.task_timeout_seconds)

        async def cancel_check() -> bool:
            return (await _batch_status(batch_id)) in CANCEL_STATUSES

        async def on_wait() -> None:
            await _transition_batch(
                batch_id, ("pending",),
                message="Waiting for free cores on the selected nodes",
            )

        async def on_update(idx: int, phase: str, info: dict) -> None:
            await _update_task(tasks[idx].id, **_task_fields(phase, info))

        allocations: list[NodeAllocation] = []
        failure: Optional[str] = None
        is_project = batch.runtime == PROJECT_RUNTIME
        try:
            project_for = None
            if is_project:
                project_for = await _prepare_project(batch, tasks, settings)
            nodes = await _select_nodes(owner_id, node_ids)
            capable = (project_nodes(nodes) if is_project
                       else select_runtime_nodes(nodes, batch.runtime))
            fitting = [n for n in capable if int(n.allocated_cpu or 0) >= cpu_per_task]
            if not nodes:
                failure = "No online provider nodes available"
            elif not capable and is_project:
                failure = "Projects need a Linux or macOS node with python3; none of the selected online nodes has both"
            elif not capable:
                failure = f"No selected online node has runtime '{batch.runtime}'"
            elif not fitting:
                failure = (
                    f"cpu_per_task={cpu_per_task} exceeds the cores allocated on every "
                    f"selected node with runtime '{batch.runtime}'"
                )
            else:
                allocations = await acquire_allocations(
                    owner_id, fitting, job_id=str(batch_id), cpu_per_task=cpu_per_task,
                    max_tasks=len(tasks), ttl_seconds=settings.lease_ttl_seconds,
                    cancel_check=cancel_check, on_wait=on_wait,
                )
                if not allocations:
                    if not await cancel_check():
                        failure = (
                            "No free capacity: every selected node is fully leased by "
                            "other jobs; try again later"
                        )
                elif await _transition_batch(batch_id, ("pending",), status="running", message=None):
                    logger.info(
                        "dispatch_batch_start", batch_id=str(batch_id), tasks=len(tasks),
                        nodes=len(allocations), slots=sum(a.slots for a in allocations),
                        runtime=batch.runtime, cpu_per_task=cpu_per_task,
                    )
                    async with lease_heartbeat(owner_id, allocations, settings.lease_ttl_seconds):
                        await run_across_nodes(
                            allocations,
                            batch.code,
                            [t.input_data for t in tasks],
                            runtime_id="python" if is_project else batch.runtime,
                            custom_command=batch.custom_command,
                            custom_filename=batch.custom_filename,
                            timeout_seconds=timeout,
                            nice=policy_nice(batch.execution_policy, settings.task_nice),
                            max_output_bytes=settings.max_output_bytes,
                            on_update=on_update,
                            cancel_check=cancel_check,
                            sweep_minutes=settings.stale_sandbox_minutes,
                            project_for=project_for,
                        )
        except ProjectSetupError as exc:
            failure = str(exc)
        except Exception as exc:
            logger.exception("dispatch_batch_error", batch_id=str(batch_id))
            failure = f"Dispatcher error: {exc}"
        finally:
            await release_allocations(owner_id, allocations)
            status = await _finalize_batch(batch_id, failure=failure)
        logger.info("dispatch_batch_done", batch_id=str(batch_id), tasks=len(tasks), status=status)


# ==============================================================================
# SECTION 6: BatchService (Persistence & Lifecycle Requests)
# ==============================================================================

class BatchService:
    def __init__(self, db_session: AsyncSession):
        self._db = db_session

    async def create_batch(
        self,
        request: BatchCreateRequest,
        owner_id: str,
        *,
        timeout_seconds: int,
    ) -> TaskBatch:
        inputs = request.inputs or [""]
        batch = TaskBatch(
            owner_id=owner_id,
            code=request.code,
            runtime=request.runtime,
            custom_command=request.custom_command if request.runtime == "custom" else None,
            custom_filename=request.custom_filename if request.runtime == "custom" else None,
            cpu_per_task=max(1, int(request.cpu_per_task or 1)),
            timeout_seconds=int(timeout_seconds),
            execution_policy=request.execution_policy,
            node_ids_json=json.dumps([str(n) for n in request.node_ids]),
            project_json=request.project.model_dump_json() if request.project else None,
            status="pending",
            total=len(inputs),
        )
        self._db.add(batch)
        await self._db.flush()
        for seq, shard in enumerate(inputs):
            self._db.add(
                Task(batch_id=batch.id, seq=seq, input_data=shard, status="pending")
            )
        await self._db.commit()
        await self._db.refresh(batch)
        logger.info(
            "batch_created", batch_id=str(batch.id), total=batch.total,
            runtime=batch.runtime, cpu_per_task=batch.cpu_per_task,
            execution_policy=batch.execution_policy,
        )
        return batch

    async def get_batch(self, batch_id: uuid.UUID, owner_id: str) -> Optional[TaskBatch]:
        result = await self._db.execute(
            select(TaskBatch).where(
                TaskBatch.id == batch_id, TaskBatch.owner_id == owner_id
            )
        )
        return result.scalar_one_or_none()

    async def list_batches(self, owner_id: str, limit: int = 20) -> list[TaskBatch]:
        result = await self._db.execute(
            select(TaskBatch)
            .where(TaskBatch.owner_id == owner_id)
            .order_by(desc(TaskBatch.created_at))
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_tasks(self, batch_id: uuid.UUID) -> list[Task]:
        result = await self._db.execute(
            select(Task).where(Task.batch_id == batch_id).order_by(Task.seq)
        )
        return list(result.scalars().all())

    async def request_cancel(self, batch: TaskBatch) -> TaskBatch:
        if batch.status in CANCELLABLE_STATUSES:
            await self._db.execute(
                update(TaskBatch)
                .where(
                    TaskBatch.id == batch.id,
                    TaskBatch.status.in_(tuple(CANCELLABLE_STATUSES)),
                )
                .values(status="cancelling")
            )
            await self._db.commit()
            await self._db.refresh(batch)
            logger.info("batch_cancel_requested", batch_id=str(batch.id), status=batch.status)
        return batch
