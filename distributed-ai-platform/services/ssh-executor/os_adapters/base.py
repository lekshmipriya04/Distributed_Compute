"""Base class, shared types and sandbox command builders for OS adapters.

Adapters are **pure**: they build command strings and parse command output, and
never open a connection, so every adapter is unit-testable without a real
machine.

Two things live here beyond the abstract interface:

* :data:`SANDBOX_RUNNER_SOURCE` — a small POSIX Python program the master
  uploads into each sandbox. It sets the clean environment, puts the task in its
  own session/process group, records that group id for cancellation, enforces the
  wall-clock timeout, kills the group afterwards and then (best effort) any
  process that left the group but still runs in the sandbox. Python 3 is the one interpreter we
  require on a worker, so this works everywhere without GNU coreutils
  (``timeout``/``setsid`` are absent on macOS).
* :class:`PosixSandboxMixin` / :class:`WindowsSandboxMixin` — the per-OS naming
  and command building, shared by the concrete adapters.
"""
from __future__ import annotations

import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from runtimes import ResourceSpec

if TYPE_CHECKING:
    # The sandbox mixins below are combined with an OSAdapter subclass, which
    # supplies these. Declaring them only for the type checker keeps the mixins
    # from shadowing the real implementations in the MRO at runtime.
    class _AdapterParts:
        os_type: str

        def python_command(self) -> str: ...
        def temp_directory(self) -> str: ...
else:
    _AdapterParts = object

#: Directory prefix for a sandbox. A random suffix is added by ``mktemp``.
SANDBOX_PREFIX = "dp-exec."

#: File inside the sandbox holding the launched process group id.
PGID_FILENAME = "runner.pgid"

#: The file the master uploads into each sandbox and runs the task through.
RUNNER_FILENAME = "dp_runner.py"

#: Name of the task input file inside the sandbox (it is also on stdin).
INPUT_FILENAME = "input.txt"

#: POSIX runner. Uploaded per execution and deleted with the sandbox.
SANDBOX_RUNNER_SOURCE = r'''"""Sandbox runner: confines one task to its throwaway sandbox.

Written by the master into the sandbox and executed with python3. It:
  * runs the task with a clean environment rooted in the sandbox,
  * puts it in its own session/process group and records that group id,
  * enforces the wall-clock timeout (exit 124, like timeout(1)),
  * kills the task's process group on timeout, cancel or normal exit,
  * then, best effort, kills processes that left the group (setsid / double
    fork) but still run inside the sandbox: on Linux those whose cwd is in the
    sandbox or whose environment carries DP_SANDBOX=<sandbox>; on macOS those
    whose cwd is in the sandbox (via lsof). A task that leaves the group AND
    changes directory (and, on Linux, clears its environment) can still escape.
"""
import json
import os
import signal
import subprocess
import sys


def _killpg(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _escaped_pids(sandbox):
    """PIDs of this user's processes that still belong to the sandbox."""
    me, parent, uid = os.getpid(), os.getppid(), os.getuid()
    root = os.path.realpath(sandbox)
    marker = ("DP_SANDBOX=" + sandbox).encode()
    found = set()
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                if os.stat("/proc/%d" % pid).st_uid != uid:
                    continue
                if _inside(os.path.realpath(os.readlink("/proc/%d/cwd" % pid)), root):
                    found.add(pid)
                    continue
            except OSError:
                pass
            try:
                with open("/proc/%d/environ" % pid, "rb") as fh:
                    if marker in fh.read().split(b"\0"):
                        found.add(pid)
            except OSError:
                pass
    else:
        try:
            out = subprocess.run(
                ["lsof", "-a", "-d", "cwd", "-u", str(uid), "-Fpn"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
            ).stdout.decode("utf-8", "replace")
        except Exception:
            out = ""
        pid = None
        for line in out.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                pid = int(line[1:])
            elif line.startswith("n") and pid is not None:
                if _inside(os.path.realpath(line[1:]), root):
                    found.add(pid)
    found.discard(me)
    found.discard(parent)
    return found


def _kill_escaped(sandbox):
    """Kill processes that left the task's group but still live in the sandbox."""
    for _ in range(3):  # repeat: a survivor may fork while we sweep
        pids = _escaped_pids(sandbox)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass


def main():
    sandbox = sys.argv[1]
    timeout_s = float(sys.argv[2])
    env = json.loads(sys.argv[3] or "{}")
    command = sys.argv[4]

    os.chdir(sandbox)
    env.setdefault("HOME", sandbox)
    env.setdefault("TMPDIR", sandbox)
    env.setdefault("PWD", sandbox)
    env["DP_SANDBOX"] = sandbox  # lets the final sweep find escaped processes

    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=sandbox,
        env=env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
        start_new_session=True,  # child becomes session+group leader
    )
    with open(os.path.join(sandbox, "runner.pgid"), "w") as fh:
        fh.write(str(proc.pid))

    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        sys.stderr.write("[task timed out]\n")
        sys.stderr.flush()
        return 124
    finally:
        # Reap anything the task left running (background jobs, children),
        # then anything that escaped the process group (setsid, daemonize).
        _killpg(proc.pid, signal.SIGKILL)
        _kill_escaped(sandbox)

    if rc is None:
        return 1
    if rc < 0:  # killed by a signal
        return 128 + (-rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
'''


#: Wrapper the master uploads next to a Python task that was given a GPU slice.
GPU_PREAMBLE_FILENAME = "dp_gpu_preamble.py"

#: Runs *before* the user's script in the same interpreter. ``python -I``
#: ignores PYTHONPATH and sitecustomize, so instead the launch line becomes
#: ``python3 -I -B dp_gpu_preamble.py task.py``: this file caps the GPU memory
#: (only if PyTorch is already installed -- nothing is ever installed) and then
#: executes the task with :func:`runpy.run_path` as ``__main__``.
#: The cap is PyTorch's caching-allocator limit: best effort, it binds torch
#: allocations only, and code that calls the setter again can raise it.
GPU_PREAMBLE_SOURCE = r'''"""dp GPU preamble: cap this process's GPU memory, then run the task."""
import os
import runpy
import sys


def _cap():
    try:
        fraction = float(os.environ.get("DP_GPU_FRACTION") or 0)
    except ValueError:
        return
    if not 0 < fraction <= 1:
        return
    try:
        import torch
    except Exception:
        return  # no PyTorch on this worker: nothing to enforce with
    try:
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                torch.cuda.set_per_process_memory_fraction(fraction, device=i)
        else:
            mps = getattr(torch, "mps", None)
            if mps is not None and hasattr(mps, "set_per_process_memory_fraction"):
                if torch.backends.mps.is_available():
                    mps.set_per_process_memory_fraction(fraction)
    except Exception as exc:
        sys.stderr.write("[dp] GPU cap not applied: %s\n" % (exc,))


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: dp_gpu_preamble.py SCRIPT [ARGS...]\n")
        return 2
    _cap()
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

#: Thread-pool variables capped to the slot's cores so BLAS/OpenMP never
#: fan out over the owner's other cores.
THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def gpu_preamble_applies(runtime: Any, spec: ResourceSpec) -> bool:
    """True when ``runtime`` is plain Python and the task has a GPU slice."""
    return getattr(runtime, "id", runtime) == "python" and spec.uses_gpu


def resource_env(runtime: Any, spec: ResourceSpec) -> dict[str, str]:
    """Thread caps + GPU visibility/caps for one task (values may be "")."""
    threads = str(spec.thread_count)
    env = {name: threads for name in THREAD_ENV_VARS}
    if gpu_preamble_applies(runtime, spec):
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in range(spec.gpu_count))
        env["DP_GPU_FRACTION"] = f"{spec.gpu_memory_fraction:.6f}"
        env["DP_GPU_MEMORY_GB"] = f"{spec.gpu_memory_gb:.3f}"
        env["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:128"
    else:
        # No enforceable slice: the task must not touch the GPU at all.
        env["CUDA_VISIBLE_DEVICES"] = ""
    return env


@dataclass
class NodeProbe:
    """Normalized result of probing a node's hardware/runtime over SSH."""

    detected_cpu: int = 0
    detected_memory_gb: float = 0.0
    detected_gpu: int = 0
    os_info: str | None = None


class OSAdapter(ABC):
    """Per-OS behaviour: probing, sandboxing and process control.

    Adapters never open connections. They return command strings for the SSH
    transport to run and parse the raw output back into normalized values.
    """

    #: Normalized OS identifier, e.g. ``"linux"``.
    os_type: str = "unknown"

    #: Whether this OS can *enforce* a CPU/memory allocation (``"strict"``) or
    #: only approximate it with priority + monitoring (``"best_effort"``).
    resource_enforcement: str = "best_effort"

    #: Shell used to run generated command strings.
    shell: str = "/bin/sh"

    # -- runtime basics ---------------------------------------------------
    @abstractmethod
    def python_command(self) -> str:
        """Interpreter invocation, e.g. ``python3`` or ``python``."""

    @abstractmethod
    def temp_directory(self) -> str:
        """Base directory for ephemeral per-execution files."""

    # -- detection: command + parser pairs --------------------------------
    @abstractmethod
    def probe_commands(self) -> dict[str, str]:
        """Map of probe name -> shell command (cpu, memory, gpu, os_info)."""

    @abstractmethod
    def parse_probe(self, raw: dict[str, str]) -> NodeProbe:
        """Turn raw probe stdout (keyed as in :meth:`probe_commands`) into a
        :class:`NodeProbe`."""

    # -- sandbox lifecycle ------------------------------------------------
    @abstractmethod
    def create_sandbox_command(self, execution_id: str) -> str:
        """Create a private sandbox; print its absolute path on stdout."""

    @abstractmethod
    def remove_sandbox_command(self, sandbox: str) -> str:
        """Recursively delete a sandbox; must never fail the caller."""

    @abstractmethod
    def build_sandbox_command(
        self,
        *,
        sandbox: str,
        runtime: Any,
        filename: str,
        spec: ResourceSpec,
        nice: int,
        env_path: str,
        custom_command: Optional[str],
        execution_id: str,
        max_output_bytes: int,
        command_override: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> str:
        """The full launch: run ``runtime`` on ``filename`` inside ``sandbox``.

        The command must run the task with a clean environment rooted in the
        sandbox, confine it (priority, optional CPU pinning, wall-clock
        timeout), attach stdin, and propagate stdout/stderr.
        ``command_override`` replaces the runtime's command (projects run their
        own command); ``extra_env`` is added to the task's environment.
        """

    @abstractmethod
    def cancel_command(self, execution_id: str) -> str:
        """Kill the running task's process group; print the sandbox if killed."""

    @abstractmethod
    def remove_sandbox_by_execution_command(self, execution_id: str) -> str:
        """Delete the sandbox belonging to an execution id."""

    @abstractmethod
    def sweep_sandboxes_command(self, older_than_minutes: int) -> str:
        """Delete stale sandboxes; print each removed path (one per line)."""

    def sandbox_runner(self) -> Optional[str]:
        """Source of the runner file to upload into each sandbox (or None)."""
        return SANDBOX_RUNNER_SOURCE

    def sandbox_support_files(self, runtime: Any, spec: ResourceSpec) -> dict[str, str]:
        """Files (name -> source) to upload into the sandbox before launch."""
        files: dict[str, str] = {}
        runner = self.sandbox_runner()
        if runner:
            files[RUNNER_FILENAME] = runner
        if runner and gpu_preamble_applies(runtime, spec):
            files[GPU_PREAMBLE_FILENAME] = GPU_PREAMBLE_SOURCE
        return files


def _safe_execution_id(execution_id: str) -> str:
    """Validate an execution id for use in a shell command and a path."""
    ident = "".join(ch for ch in str(execution_id or "") if ch.isalnum() or ch in "._-")
    if not ident:
        raise ValueError("execution_id must contain at least one safe character")
    return ident[:64]


def _env_for(
    runtime: Any, sandbox: str, env_path: str, timeout_seconds: int,
    spec: Optional[ResourceSpec] = None,
) -> dict:
    """Build the clean environment the task runs with, rooted in the sandbox."""
    from runtimes import sandbox_env_for

    env: dict[str, str] = {
        "HOME": sandbox,
        "TMPDIR": sandbox,
        "PWD": sandbox,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if env_path:
        env["PATH"] = env_path
    env.update(sandbox_env_for(runtime, sandbox))
    env = {k: v for k, v in env.items() if v}
    if spec is not None:
        # Added after the filter: CUDA_VISIBLE_DEVICES="" must survive.
        env.update(resource_env(runtime, spec))
    return env


class PosixSandboxMixin:
    """Sandbox command building for Linux and macOS (POSIX shells)."""

    shell = "/bin/sh"

    # -- sandbox paths ----------------------------------------------------
    def create_sandbox_command(self, execution_id: str) -> str:
        ident = _safe_execution_id(execution_id)
        base = self.temp_directory().rstrip("/")
        return f'mkdir -p {shlex.quote(base)} && mktemp -d {shlex.quote(f"{base}/dp-exec.{ident}.XXXXXX")}'

    def remove_sandbox_command(self, sandbox: str) -> str:
        quoted = shlex.quote(sandbox)
        return f'rm -rf {quoted} 2>/dev/null; true'

    def _sandbox_glob(self, execution_id: str) -> str:
        """Shell glob matching this execution's sandbox(es).

        Only the base directory is quoted: the ``*`` must stay live for the
        shell, and the id is already restricted to ``[A-Za-z0-9._-]``.
        """
        ident = _safe_execution_id(execution_id)
        base = shlex.quote(self.temp_directory().rstrip("/"))
        return f"{base}/dp-exec.{ident}.*"

    def remove_sandbox_by_execution_command(self, execution_id: str) -> str:
        pattern = self._sandbox_glob(execution_id)
        return (
            f"for d in {pattern}; do "
            '[ -d "$d" ] && rm -rf "$d" 2>/dev/null; '
            "done; true"
        )

    def cancel_command(self, execution_id: str) -> str:
        """Kill the task's process group (TERM, then KILL) and report it."""
        pattern = self._sandbox_glob(execution_id)
        return (
            f"for d in {pattern}; do "
            '[ -d "$d" ] || continue; '
            f'pgid=$(cat "$d/{PGID_FILENAME}" 2>/dev/null || true); '
            'if [ -n "$pgid" ]; then '
            'kill -TERM -"$pgid" 2>/dev/null && echo "$d"; '
            'sleep 1; kill -KILL -"$pgid" 2>/dev/null; '
            "fi; "
            "done; true"
        )

    def sweep_sandboxes_command(self, older_than_minutes: int) -> str:
        base = shlex.quote(self.temp_directory().rstrip("/"))
        minutes = max(1, int(older_than_minutes))
        return (
            f'find {base} -maxdepth 1 -name "dp-exec.*" -type d '
            f'-mmin +{minutes} 2>/dev/null | while IFS= read -r d; do '
            'rm -rf "$d" 2>/dev/null && echo "$d"; done; true'
        )

    # -- launch -----------------------------------------------------------
    def build_sandbox_command(
        self,
        *,
        sandbox: str,
        runtime: Any,
        filename: str,
        spec: ResourceSpec,
        nice: int,
        env_path: str,
        custom_command: Optional[str],
        execution_id: str,
        max_output_bytes: int = 1_000_000,
        command_override: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> str:
        """Run the runtime's command through the sandbox runner.

        ``transport.exec`` feeds stdin to this command and reads its stdout, so
        the runner is addressed by absolute path and everything else happens
        inside the sandbox.
        """
        from runtimes import build_run_command

        if command_override is not None:
            inner = command_override
        elif gpu_preamble_applies(runtime, spec):
            # -I drops PYTHONPATH/sitecustomize, so the preamble is the script
            # python runs; it then runs the task file via runpy.
            tool = runtime.tool_names[0] if getattr(runtime, "tool_names", None) else "python3"
            inner = f"{tool} -I -B {GPU_PREAMBLE_FILENAME} {shlex.quote(filename)}"
        else:
            inner = build_run_command(
                runtime, filename=filename, os_type=self.os_type, custom_command=custom_command
            )
        # nice wraps a shell so compound commands (build && run) are all niced.
        inner = f"nice -n {int(nice)} /bin/sh -c {shlex.quote(inner)}"
        inner = self._pin_cores(inner, spec)
        env = _env_for(runtime, sandbox, env_path, spec.timeout_seconds, spec)
        env.update(extra_env or {})
        env_json = _json_dumps(env)
        runner = f"{sandbox.rstrip('/')}/{RUNNER_FILENAME}"
        return (
            f"{shlex.quote(self.python_command())} {shlex.quote(runner)} "
            f"{shlex.quote(sandbox)} {int(spec.timeout_seconds)} "
            f"{shlex.quote(env_json)} {shlex.quote(inner)}"
        )

    def _pin_cores(self, command: str, spec: ResourceSpec) -> str:
        """Pin the task to its own core slice where the OS supports it."""
        cores = spec.core_list
        if not cores:
            return command
        return (
            f"if command -v taskset >/dev/null 2>&1; then "
            f"taskset -c {shlex.quote(cores)} {command}; else {command}; fi"
        )

    # -- legacy helper (kept so older callers keep working) ---------------
    def build_execute_command(
        self, remote_path: str, spec: ResourceSpec, *, nice: int = 19, strict: bool = False
    ) -> str:
        """Run an already-uploaded script with priority + a wall-clock bound.

        Prefer :meth:`build_sandbox_command`, which adds the sandbox-rooted
        environment and process-group control. This remains for the local
        single-node execution path and for callers that manage their own files.
        """
        quoted = shlex.quote(remote_path)
        confine = f"nice -n {nice} {self.python_command()} {quoted}"
        return (
            f"if command -v taskset >/dev/null 2>&1; then "
            f"taskset -c {spec.core_list} {confine}; else {confine}; fi"
        )


class WindowsSandboxMixin:
    """Sandbox command building for Windows OpenSSH + PowerShell.

    NOT verified on a real Windows host: written from the documented behaviour
    of OpenSSH on Windows and PowerShell. Treated as best-effort.
    """

    shell = "powershell"

    def _ps(self, script: str) -> str:
        import base64

        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"

    def create_sandbox_command(self, execution_id: str) -> str:
        ident = _safe_execution_id(execution_id)
        script = (
            f"$sb = Join-Path $env:TEMP ('dp-exec.{ident}.' + [guid]::NewGuid().ToString('N').Substring(0,8)); "
            "New-Item -ItemType Directory -Force -Path $sb | Out-Null; "
            "Write-Output $sb"
        )
        return self._ps(script)

    def remove_sandbox_command(self, sandbox: str) -> str:
        path = sandbox.replace("'", "''")
        return self._ps(
            f"Remove-Item -Recurse -Force -LiteralPath '{path}' -ErrorAction SilentlyContinue"
        )

    def remove_sandbox_by_execution_command(self, execution_id: str) -> str:
        ident = _safe_execution_id(execution_id)
        return self._ps(
            f"Get-ChildItem -Path $env:TEMP -Directory -Filter 'dp-exec.{ident}.*' "
            "-ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"
        )

    def cancel_command(self, execution_id: str) -> str:
        ident = _safe_execution_id(execution_id)
        return self._ps(
            f"Get-ChildItem -Path $env:TEMP -Directory -Filter 'dp-exec.{ident}.*' "
            "-ErrorAction SilentlyContinue | ForEach-Object { "
            "$pidFile = Join-Path $_.FullName 'runner.pgid'; "
            "if (Test-Path $pidFile) { $p = Get-Content $pidFile; "
            "taskkill /PID $p /T /F 2>$null | Out-Null; Write-Output $_.FullName } }"
        )

    def sweep_sandboxes_command(self, older_than_minutes: int) -> str:
        minutes = max(1, int(older_than_minutes))
        return self._ps(
            "$cut = (Get-Date).AddMinutes(-"
            + str(minutes)
            + "); "
            "Get-ChildItem -Path $env:TEMP -Directory -Filter 'dp-exec.*' -ErrorAction SilentlyContinue | "
            "Where-Object { $_.LastWriteTime -lt $cut } | "
            "ForEach-Object { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue; Write-Output $_.FullName }"
        )

    def build_sandbox_command(
        self,
        *,
        sandbox: str,
        runtime: Any,
        filename: str,
        spec: ResourceSpec,
        nice: int,
        env_path: str,
        custom_command: Optional[str],
        execution_id: str,
        max_output_bytes: int = 1_000_000,
        command_override: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> str:
        from runtimes import build_run_command

        inner = build_run_command(
            runtime, filename=filename, os_type="windows", custom_command=custom_command
        )
        sb = sandbox.replace("'", "''")
        cmd = inner.replace("'", "''")
        ms = int(spec.timeout_seconds) * 1000
        script = (
            f"$sb = '{sb}'; "
            f"$cmd = '{cmd}'; "
            "Set-Location -LiteralPath $sb; "
            "$env:HOME = $sb; $env:TMPDIR = $sb; $env:TEMP = $sb; $env:TMP = $sb; "
            f"$out = Join-Path $sb 'stdout.txt'; $err = Join-Path $sb 'stderr.txt'; "
            "$p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $cmd "
            "-WorkingDirectory $sb -NoNewWindow -PassThru "
            "-RedirectStandardOutput $out -RedirectStandardError $err; "
            f"Set-Content -LiteralPath (Join-Path $sb 'runner.pgid') -Value $p.Id; "
            f"if (-not $p.WaitForExit({ms})) {{ "
            "taskkill /PID $p.Id /T /F 2>$null | Out-Null; "
            "[Console]::Error.WriteLine('[task timed out]'); "
            "Get-Content -LiteralPath $out -ErrorAction SilentlyContinue; "
            "Get-Content -LiteralPath $err -ErrorAction SilentlyContinue; "
            "exit 124 }; "
            "Get-Content -LiteralPath $out -ErrorAction SilentlyContinue; "
            "Get-Content -LiteralPath $err -ErrorAction SilentlyContinue; "
            "exit $p.ExitCode"
        )
        return self._ps(script)

    def sandbox_runner(self) -> Optional[str]:
        return None  # Windows uses the inline PowerShell above

    def build_execute_command(
        self, remote_path: str, spec: ResourceSpec, *, nice: int = 19, strict: bool = False
    ) -> str:
        path = remote_path.replace("'", "''")
        ms = spec.timeout_seconds * 1000
        return self._ps(
            f"$p = Start-Process -FilePath '{self.python_command()}' "
            f"-ArgumentList '{path}' -NoNewWindow -PassThru; "
            f"if (-not $p.WaitForExit({ms})) {{ $p.Kill(); exit 124 }} "
            "exit $p.ExitCode"
        )


def _json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"))
