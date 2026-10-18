"""Provider nodes: password-only onboarding with platform-managed keys.

Flow (see docs/API_CONTRACT.md §1):

1. ``connect``   — bare SSH handshake, read the host key only (no credential is
   sent), return its fingerprint + a signed onboarding token binding it.
2. ``provision`` — log in once with the password over the *pinned* host key,
   require python3, generate an Ed25519 key on the master, append
   ``restrict <pubkey> dp-platform:<provision_id>`` to the node's authorized
   keys, disconnect, log in again with the key only, detect capabilities and
   keep the encrypted private key in a ``pending_provisions`` row. The password
   lives only in this request's memory: it is never stored or logged.
   ``provision_manual`` + ``verify_provision`` are the fallback for nodes whose
   sshd refuses passwords: the user pastes the install command themselves.
3. ``register``  — turn the pending row into an :class:`SSHNode`
   (``auth_type="managed_key"``, ``id == provision_id``).

Blocking paramiko work runs in worker threads (``asyncio.to_thread``).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import paramiko
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import base64
from dataclasses import asdict, dataclass, field
import runtimes as runtime_registry
from os_adapters.base import OSAdapter
from config import get_settings
from security import decrypt_secret, encrypt_secret
from database import get_db_manager
from ssh import (
    HostKeyFetchError,
    HostKeyMismatch,
    InvalidOnboardingToken,
    ManagedKey,
    OnboardingClaims,
    SSHTransport,
    authorized_key_line,
    fetch_host_key,
    fingerprint,
    generate_keypair,
    install_authorized_key,
    issue_onboarding_token,
    key_comment,
    manual_install_commands,
    remove_authorized_key,
    verify_onboarding_token,
)
from models import PendingProvision, SSHNode
from runtimes import clamp_gpu_allocation
from schemas import (
    NodeConnectRequest,
    NodeManualProvisionRequest,
    NodeProvisionRequest,
    NodeRegisterRequest,
    NodeResecureRequest,
    NodeUpdateRequest,
)
from tasks import detect_os, select_adapter

logger = structlog.get_logger(__name__)

MANAGED_KEY = "managed_key"
LEGACY_AUTH_TYPES = ("password", "private_key")
_CAPS_TIMEOUT = 20
_KEY_OP_TIMEOUT = 30


def _remove_worker_cache(t) -> None:
    """Delete cached project environments (best effort; the key removal matters more)."""
    from projects import WORKER_CACHE_DIR

    try:
        t.exec(f'rm -rf "$HOME"/{WORKER_CACHE_DIR}', timeout=_KEY_OP_TIMEOUT)
    except Exception as exc:
        logger.warning("worker_cache_removal_failed", error=str(exc))


# --- errors (mapped to HTTP in routes_nodes) ---------------------------------
class NodeServiceError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NodeNotFound(NodeServiceError):
    status_code = 404


class OnboardingAuthFailed(NodeServiceError):
    status_code = 401

    def __init__(self, detail: str = "Authentication failed"):
        super().__init__(detail)


class PasswordAuthDisabled(NodeServiceError):
    status_code = 409


class HostKeyChanged(NodeServiceError):
    status_code = 409


class KeyRejected(NodeServiceError):
    status_code = 409


class NodeStateConflict(NodeServiceError):
    status_code = 409


class HostNotAllowed(NodeServiceError):
    status_code = 403


class NodeUnreachable(NodeServiceError):
    status_code = 502


class PrerequisiteMissing(NodeServiceError):
    status_code = 400


class InvalidOnboarding(NodeServiceError):
    status_code = 400


def _connect(transport: SSHTransport, *, key_auth: bool = False) -> dict:
    """Open ``transport``; translate paramiko failures into service errors.

    Never includes a credential in the error text.
    """
    try:
        return transport.connect()
    except (HostKeyMismatch, paramiko.BadHostKeyException) as exc:
        raise HostKeyChanged(
            "host_key_mismatch: the node presented a different host key than the pinned one. "
            "If the machine was reinstalled, remove it and add it again."
        ) from exc
    except paramiko.BadAuthenticationType as exc:
        allowed = set(getattr(exc, "allowed_types", None) or [])
        if not key_auth and not allowed & {"password", "keyboard-interactive"}:
            raise PasswordAuthDisabled(
                "password_auth_disabled: the node's SSH server does not accept passwords "
                f"(allowed: {', '.join(sorted(allowed)) or 'none'}). Use the manual key install instead."
            ) from exc
        if key_auth:
            raise KeyRejected("key_rejected: the node no longer accepts the platform key") from exc
        raise OnboardingAuthFailed() from exc
    except paramiko.AuthenticationException as exc:
        if key_auth:
            raise KeyRejected("key_rejected: the node no longer accepts the platform key") from exc
        raise OnboardingAuthFailed() from exc
    except (OSError, EOFError, paramiko.SSHException) as exc:
        raise NodeUnreachable(
            f"node_unreachable: could not open an SSH session to {transport.host}:{transport.port} ({exc})"
        ) from exc


# --- host guard (SSRF) ---------------------------------------------------------
def _resolve_addresses(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        if addr not in seen:
            seen.append(addr)
    return seen


async def check_host_allowed(host: str) -> str:
    """Apply allowed_hosts / allowed_cidrs / loopback+link-local blocking to
    every address ``host`` resolves to. Returns the address to connect to (the
    checked one, so a DNS change between check and connect cannot bypass it)."""
    settings = get_settings()
    host = (host or "").strip()
    if not host:
        raise InvalidOnboarding("Host is required")
    if settings.allowed_hosts and host.lower() not in {h.lower() for h in settings.allowed_hosts}:
        raise HostNotAllowed(f"Host {host} is not in the provider allowlist")
    try:
        addresses = await asyncio.to_thread(_resolve_addresses, host)
    except (socket.gaierror, UnicodeError) as exc:
        raise InvalidOnboarding(f"Could not resolve host {host}") from exc
    if not addresses:
        raise InvalidOnboarding(f"Could not resolve host {host}")
    networks = [ipaddress.ip_network(c, strict=False) for c in settings.allowed_cidrs]
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if settings.block_loopback_and_link_local and (
            ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
        ):
            raise HostNotAllowed(f"Host {host} resolves to a blocked address ({ip})")
        if networks and not any(ip.version == n.version and ip in n for n in networks):
            raise HostNotAllowed(f"Host {host} ({ip}) is outside the allowed networks")
    return addresses[0]


# --- capabilities ----------------------------------------------------------------
_PATH_MARK = "__DP_ENV_PATH__"

GPU_RUNTIME_PROBE = (
    "try:\n"
    "    import torch\n"
    "    if torch.cuda.is_available():\n"
    "        print('torch-cuda')\n"
    "    elif getattr(getattr(torch, 'backends', None), 'mps', None) is not None "
    "and torch.backends.mps.is_available():\n"
    "        print('torch-mps')\n"
    "except Exception:\n"
    "    pass\n"
)
_GPU_RUNTIMES = frozenset({"torch-cuda", "torch-mps"})


@dataclass
class NodeCapabilities:
    os_type: str
    os_version: Optional[str] = None
    cpu_total: int = 0
    memory_total_gb: float = 0.0
    gpu_vendor: Optional[str] = None
    gpu_backend: Optional[str] = None
    gpu_model: Optional[str] = None
    gpu_count: int = 0
    gpu_memory_gb: Optional[float] = None
    gpu_runtime: Optional[str] = None
    python_version: Optional[str] = None
    runtimes: dict[str, str] = field(default_factory=dict)
    env_path: Optional[str] = None
    execution_backend: str = "ssh"
    resource_enforcement: str = "best_effort"

    def to_dict(self) -> dict:
        return asdict(self)


def _run_probe(transport: Any, command: str, timeout: int, input_data: str = "") -> str:
    try:
        return (
            transport.exec(command, input_data=input_data, timeout=timeout).get("stdout") or ""
        ).strip()
    except Exception:
        return ""


def _search_probe(pattern: str, text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _mib_to_gb(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)", text or "")
    if not m:
        return None
    return round(float(m.group(1)) / 1024.0, 2)


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _ps_encoded(script: str) -> str:
    b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {b64}"


def parse_env_path(output: str) -> Optional[str]:
    m = re.search(re.escape(_PATH_MARK) + r"(.*?)" + re.escape(_PATH_MARK), output or "", re.S)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def parse_gpu_runtime(output: str) -> Optional[str]:
    for line in (output or "").splitlines():
        if line.strip() in _GPU_RUNTIMES:
            return line.strip()
    return None


def env_path_command(os_type: str) -> str:
    if os_type == "windows":
        return _ps_encoded(f"Write-Output ('{_PATH_MARK}' + $env:Path + '{_PATH_MARK}')")
    script = f'printf "%s%s%s" {_PATH_MARK} "$PATH" {_PATH_MARK}'
    return f'"${{SHELL:-/bin/sh}}" -lc {_sh_quote(script)} </dev/null 2>/dev/null'


def runtime_detection_command(os_type: str, env_path: Optional[str]) -> str:
    if os_type == "windows":
        script = runtime_registry.windows_detection_script()
        if env_path:
            script = f"$env:Path = '{env_path.replace(chr(39), chr(39) * 2)}'\n" + script
        return _ps_encoded(script)
    script = runtime_registry.detection_script()
    prefix = f"PATH={_sh_quote(env_path)}; export PATH; " if env_path else ""
    return f"{prefix}sh -c {_sh_quote(script)}"


def detect_capabilities(
    transport: Any, adapter: OSAdapter, timeout: int = 20
) -> NodeCapabilities:
    raw = {name: _run_probe(transport, cmd, timeout) for name, cmd in adapter.probe_commands().items()}
    try:
        probe = adapter.parse_probe(raw)
    except Exception:
        probe = None

    py = adapter.python_command()
    os_type = adapter.os_type

    env_path = parse_env_path(_run_probe(transport, env_path_command(os_type), timeout))
    if not env_path:
        default_path = getattr(adapter, "default_path", None)
        env_path = default_path() if callable(default_path) else None
    found = runtime_registry.parse_detection_output(
        _run_probe(transport, runtime_detection_command(os_type, env_path), timeout)
    )
    python_version = _search_probe(r"Python\s+([\d.]+)", found.get("python", ""))
    if "python" not in found:
        python_version = _search_probe(r"Python\s+([\d.]+)", _run_probe(transport, f"{py} --version 2>&1", timeout))
        if python_version:
            found["python"] = f"Python {python_version}"

    gpu_vendor = gpu_backend = gpu_model = None
    gpu_count = int(getattr(probe, "detected_gpu", 0) or 0)
    gpu_memory_gb: Optional[float] = None

    if os_type == "linux":
        nv = _run_probe(transport, "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null", timeout)
        if nv:
            lines = [l for l in nv.splitlines() if l.strip()]
            gpu_count = max(gpu_count, len(lines))
            gpu_vendor, gpu_backend = "nvidia", "cuda"
            if lines:
                parts = lines[0].split(",")
                gpu_model = parts[0].strip() or None
                if len(parts) > 1:
                    gpu_memory_gb = _mib_to_gb(parts[1])
    elif os_type == "macos":
        if gpu_count:
            gpu_vendor, gpu_backend = "apple", "metal"
    elif os_type == "windows":
        name = _run_probe(transport, 'powershell -NoProfile -Command "(Get-CimInstance Win32_VideoController | Select-Object -First 1).Name"', timeout)
        if name:
            gpu_model = name.strip()
            gpu_count = max(gpu_count, 1)
            low = gpu_model.lower()
            if "nvidia" in low:
                gpu_vendor, gpu_backend = "nvidia", "cuda"
                nv = _run_probe(transport, "nvidia-smi --query-gpu=memory.total --format=csv,noheader", timeout)
                if nv:
                    gpu_memory_gb = _mib_to_gb(nv.splitlines()[0])
            elif "amd" in low or "radeon" in low:
                gpu_vendor, gpu_backend = "amd", "directml"
            elif "intel" in low:
                gpu_vendor, gpu_backend = "intel", "directml"

    gpu_runtime: Optional[str] = None
    if python_version and gpu_count:
        prefix = f"PATH={_sh_quote(env_path)}; export PATH; " if env_path and os_type != "windows" else ""
        gpu_runtime = parse_gpu_runtime(
            _run_probe(transport, f"{prefix}{py} -I -", max(timeout, 60), input_data=GPU_RUNTIME_PROBE)
        )

    return NodeCapabilities(
        os_type=os_type,
        os_version=getattr(probe, "os_info", None),
        cpu_total=int(getattr(probe, "detected_cpu", 0) or 0),
        memory_total_gb=float(getattr(probe, "detected_memory_gb", 0.0) or 0.0),
        gpu_vendor=gpu_vendor,
        gpu_backend=gpu_backend,
        gpu_model=gpu_model,
        gpu_count=gpu_count,
        gpu_memory_gb=gpu_memory_gb,
        gpu_runtime=gpu_runtime,
        python_version=python_version,
        runtimes=found,
        env_path=env_path,
        execution_backend="ssh",
        resource_enforcement=getattr(adapter, "resource_enforcement", "best_effort") or "best_effort",
    )


def _caps_to_dict(caps: Any) -> dict:
    """Normalise a NodeCapabilities (old or new shape) into a JSON-able dict."""
    g = lambda name, default=None: getattr(caps, name, default)  # noqa: E731
    runtimes = dict(g("runtimes") or {})
    python_version = g("python_version")
    if python_version and "python" not in runtimes:
        runtimes["python"] = python_version
    return {
        "os_type": g("os_type"),
        "os_version": g("os_version"),
        "cpu_total": int(g("cpu_total", 0) or 0),
        "memory_total_gb": float(g("memory_total_gb", 0.0) or 0.0),
        "gpu_vendor": g("gpu_vendor"),
        "gpu_backend": g("gpu_backend"),
        "gpu_model": g("gpu_model"),
        "gpu_count": int(g("gpu_count", 0) or 0),
        "gpu_memory_gb": g("gpu_memory_gb"),
        "gpu_runtime": g("gpu_runtime"),
        "python_version": python_version,
        "runtimes": {str(k): str(v) for k, v in runtimes.items()},
        "env_path": g("env_path"),
        "execution_backend": g("execution_backend") or "ssh",
        "resource_enforcement": g("resource_enforcement") or "best_effort",
    }


def capability_fields(caps: dict) -> dict:
    """Map a capabilities dict onto the API's ``_CapabilityFields`` names."""
    return {
        "os_type": caps.get("os_type"),
        "os_info": caps.get("os_version"),
        "detected_cpu": int(caps.get("cpu_total") or 0),
        "detected_memory_gb": float(caps.get("memory_total_gb") or 0.0),
        "detected_gpu": int(caps.get("gpu_count") or 0),
        "gpu_vendor": caps.get("gpu_vendor"),
        "gpu_backend": caps.get("gpu_backend"),
        "gpu_model": caps.get("gpu_model"),
        "gpu_count": int(caps.get("gpu_count") or 0),
        "gpu_memory_gb": caps.get("gpu_memory_gb"),
        "gpu_runtime": caps.get("gpu_runtime"),
        "python_version": caps.get("python_version"),
        "runtimes": dict(caps.get("runtimes") or {}),
        "execution_backend": caps.get("execution_backend") or "ssh",
        "resource_enforcement": caps.get("resource_enforcement") or "best_effort",
    }


def _apply_caps(node: SSHNode, caps: dict) -> None:
    node.os_type = caps.get("os_type")
    node.os_info = caps.get("os_version")
    node.detected_cpu = int(caps.get("cpu_total") or 0)
    node.detected_memory_gb = float(caps.get("memory_total_gb") or 0.0)
    node.detected_gpu = int(caps.get("gpu_count") or 0)
    node.gpu_vendor = caps.get("gpu_vendor")
    node.gpu_backend = caps.get("gpu_backend")
    node.gpu_model = caps.get("gpu_model")
    node.gpu_count = int(caps.get("gpu_count") or 0)
    node.gpu_memory_gb = caps.get("gpu_memory_gb")
    node.gpu_runtime = caps.get("gpu_runtime")
    node.python_version = caps.get("python_version")
    node.runtimes_json = json.dumps(caps.get("runtimes") or {})
    node.env_path = caps.get("env_path")
    node.execution_backend = caps.get("execution_backend") or "ssh"
    node.resource_enforcement = caps.get("resource_enforcement") or "best_effort"
    node.capabilities_json = json.dumps(caps)


def _clamp_allocation(cpu: int, memory_gb: float, detected_cpu: int, detected_mem: float) -> tuple[int, float]:
    if detected_cpu:
        cpu = min(cpu, detected_cpu)
    if detected_mem:
        memory_gb = min(memory_gb, detected_mem)
    return max(1, int(cpu)), max(0.0, float(memory_gb))


def _clamp_gpu(node: SSHNode, count: Optional[int] = None, memory_gb: Optional[float] = None) -> None:
    """Set (or re-clamp) the node's GPU slice against its detected GPU."""
    node.allocated_gpu_count, node.allocated_gpu_memory_gb = clamp_gpu_allocation(
        node.allocated_gpu_count if count is None else count,
        node.allocated_gpu_memory_gb if memory_gb is None else memory_gb,
        node.gpu_count,
        node.gpu_memory_gb,
    )


def _require_python(transport: SSHTransport, adapter, timeout: int) -> str:
    """Fail with a clear message unless a Python 3 interpreter is on PATH."""
    py = adapter.python_command()
    res = transport.exec(f"{py} --version", timeout=timeout)
    text = f"{res.get('stdout') or ''}\n{res.get('stderr') or ''}"
    m = re.search(r"Python\s+(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m or int(m.group(1)) < 3:
        raise PrerequisiteMissing(
            f"python3 is required on the node but `{py}` was not found (or is not Python 3). "
            "Install Python 3 on the worker and try again."
        )
    return ".".join(g for g in m.groups() if g)


def _detect(transport: SSHTransport, adapter) -> dict:
    return _caps_to_dict(detect_capabilities(transport, adapter, timeout=_CAPS_TIMEOUT))


# --- transports ------------------------------------------------------------------
def transport_for_node(node: SSHNode, *, host: Optional[str] = None) -> SSHTransport:
    """Build an *unconnected* transport for a stored node.

    Managed nodes authenticate with the platform key; legacy rows with their
    stored password / private key. The host key is pinned when known.
    """
    settings = get_settings()
    secret = decrypt_secret(node.secret_encrypted)
    use_key = node.auth_type in (MANAGED_KEY, "private_key")
    return SSHTransport(
        host=host or node.host,
        port=node.port,
        username=node.username,
        password=None if use_key else secret,
        private_key=secret if use_key else None,
        host_key_type=node.host_key_type,
        host_key_b64=node.host_key_b64,
        connect_timeout=settings.connect_timeout,
    )


def _key_transport(host: str, port: int, username: str, private_key: str,
                   host_key_type: Optional[str], host_key_b64: Optional[str]) -> SSHTransport:
    return SSHTransport(
        host=host, port=port, username=username, password=None, private_key=private_key,
        host_key_type=host_key_type, host_key_b64=host_key_b64,
        connect_timeout=get_settings().connect_timeout,
    )


def _best_effort_remove(transport: SSHTransport, os_type: str, comment: str,
                        key_blob: Optional[str], *, key_auth: bool) -> bool:
    try:
        if not transport.is_connected:
            _connect(transport, key_auth=key_auth)
        remove_authorized_key(transport, os_type, comment, key_blob, timeout=_KEY_OP_TIMEOUT)
        return True
    except Exception as exc:
        logger.warning("ssh_key_rollback_failed", host=transport.host, comment=comment, error=str(exc))
        return False
    finally:
        transport.close()


def _install_and_verify(
    cred_transport: SSHTransport, *, comment: str, key_blob_to_replace: Optional[str] = None,
    require_python: bool = True, cred_is_key: bool = False,
) -> tuple[ManagedKey, dict, dict]:
    """Shared core of provision / resecure (blocking).

    Logs in with ``cred_transport`` (password or legacy credential), installs a
    freshly generated managed key, disconnects, logs in with the new key only,
    and detects capabilities over that key session. On any failure after the
    install, the new line is removed again with the original credential.

    Returns ``(key, capabilities, host_key_info)``.
    """
    settings = get_settings()
    timeout = settings.connect_timeout
    host_key = _connect(cred_transport, key_auth=cred_is_key)
    try:
        os_type = detect_os(cred_transport, timeout=timeout)
        adapter = select_adapter(os_type)
        if require_python:
            _require_python(cred_transport, adapter, timeout)
        key = generate_keypair()
        line = authorized_key_line(key.public_key, comment)
        try:
            install_authorized_key(cred_transport, os_type, line, comment, timeout=_KEY_OP_TIMEOUT)
        except Exception as exc:
            raise NodeServiceError(f"Could not install the platform key on the node: {exc}") from exc
    finally:
        cred_transport.close()

    key_t = _key_transport(cred_transport.host, cred_transport.port, cred_transport.username,
                           key.private_key_pem, host_key["host_key_type"], host_key["host_key_b64"])
    try:
        try:
            _connect(key_t, key_auth=True)
        except KeyRejected as exc:
            raise NodeServiceError(
                "The key was installed but the node did not accept it. The SSH server may use a "
                "non-default AuthorizedKeysFile or have public-key login disabled."
            ) from exc
        caps = _detect(key_t, adapter)
        if require_python and not caps.get("python_version"):
            raise PrerequisiteMissing("python3 was not found on the node over the key login")
        if key_blob_to_replace:
            remove_authorized_key(key_t, os_type, comment, key_blob_to_replace, timeout=_KEY_OP_TIMEOUT)
    except Exception:
        _best_effort_remove(cred_transport, os_type, comment, key.key_blob, key_auth=cred_is_key)
        raise
    finally:
        key_t.close()
    caps.setdefault("os_type", os_type)
    if not caps.get("os_type"):
        caps["os_type"] = os_type
    return key, caps, host_key


class NodeService:
    def __init__(self, db_session: AsyncSession):
        self._db = db_session
        self._settings = get_settings()

    # -- helpers ---------------------------------------------------------------------
    def _claims(self, token: str, owner_id: str) -> OnboardingClaims:
        try:
            return verify_onboarding_token(token, owner_id)
        except InvalidOnboardingToken as exc:
            raise InvalidOnboarding(str(exc)) from exc

    async def _get_pending(self, provision_id: uuid.UUID, owner_id: str) -> PendingProvision:
        res = await self._db.execute(
            select(PendingProvision).where(
                PendingProvision.id == provision_id, PendingProvision.owner_id == owner_id
            )
        )
        pending = res.scalar_one_or_none()
        if pending is None:
            raise NodeNotFound("Provision not found")
        expires_at = pending.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise InvalidOnboarding("Provision expired; connect to the node again")
        return pending

    def _new_expiry(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self._settings.pending_provision_ttl_seconds)

    @staticmethod
    def _provision_response(pending: PendingProvision, caps: dict) -> dict:
        return {
            "provision_id": pending.id,
            "host": pending.host,
            "port": pending.port,
            "username": pending.username,
            "fingerprint": fingerprint(pending.host_key_type, pending.host_key_b64),
            "expires_at": pending.expires_at,
            **capability_fields(caps),
        }

    # -- 1. connect: host key only, no credential -----------------------------------
    async def connect(self, request: NodeConnectRequest, owner_id: str) -> dict:
        address = await check_host_allowed(request.host)
        try:
            info = await asyncio.to_thread(
                fetch_host_key, address, request.port, self._settings.connect_timeout
            )
        except HostKeyFetchError as exc:
            raise NodeUnreachable(f"node_unreachable: {exc}") from exc
        token = issue_onboarding_token(
            owner_id=owner_id, host=request.host.strip(), port=request.port,
            username=request.username, host_key_type=info["host_key_type"],
            host_key_b64=info["host_key_b64"],
        )
        count = await self._db.scalar(
            select(func.count()).select_from(SSHNode).where(
                SSHNode.owner_id == owner_id, SSHNode.host_key_b64 == info["host_key_b64"]
            )
        )
        logger.info("ssh_node_connect", host=request.host, port=request.port, fingerprint=info["fingerprint"])
        return {
            "host": request.host.strip(),
            "port": request.port,
            "username": request.username,
            "host_key_type": info["host_key_type"],
            "fingerprint": info["fingerprint"],
            "onboarding_token": token,
            "already_registered": bool(count),
        }

    # -- 2. provision: one password login -> managed key ------------------------------
    async def provision(self, request: NodeProvisionRequest, owner_id: str) -> dict:
        claims = self._claims(request.onboarding_token, owner_id)
        address = await check_host_allowed(claims.host)
        provision_id = uuid.uuid4()
        comment = key_comment(provision_id)
        cred = SSHTransport(
            host=address, port=claims.port, username=claims.username,
            password=request.password,
            host_key_type=claims.host_key_type, host_key_b64=claims.host_key_b64,
            connect_timeout=self._settings.connect_timeout,
        )
        key, caps, _ = await asyncio.to_thread(_install_and_verify, cred, comment=comment)
        del cred  # drop the only object holding the password

        pending = PendingProvision(
            id=provision_id, owner_id=owner_id, host=claims.host, port=claims.port,
            username=claims.username, host_key_type=claims.host_key_type,
            host_key_b64=claims.host_key_b64,
            private_key_encrypted=encrypt_secret(key.private_key_pem),
            public_key=key.public_key, key_comment=comment, status="installed",
            capabilities_json=json.dumps(caps), expires_at=self._new_expiry(),
        )
        try:
            self._db.add(pending)
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            await asyncio.to_thread(
                _best_effort_remove,
                _key_transport(address, claims.port, claims.username, key.private_key_pem,
                               claims.host_key_type, claims.host_key_b64),
                caps.get("os_type") or "linux", comment, None, key_auth=True,
            )
            raise
        logger.info("ssh_node_provisioned", provision_id=str(provision_id), host=claims.host,
                    os=caps.get("os_type"), key_fingerprint=key.fingerprint)
        return self._provision_response(pending, caps)

    # -- 2b/2c. manual fallback -------------------------------------------------------
    async def provision_manual(self, request: NodeManualProvisionRequest, owner_id: str) -> dict:
        claims = self._claims(request.onboarding_token, owner_id)
        await check_host_allowed(claims.host)
        provision_id = uuid.uuid4()
        comment = key_comment(provision_id)
        key = generate_keypair()
        line = authorized_key_line(key.public_key, comment)
        posix_cmd, windows_cmd = manual_install_commands(line, comment)
        pending = PendingProvision(
            id=provision_id, owner_id=owner_id, host=claims.host, port=claims.port,
            username=claims.username, host_key_type=claims.host_key_type,
            host_key_b64=claims.host_key_b64,
            private_key_encrypted=encrypt_secret(key.private_key_pem),
            public_key=key.public_key, key_comment=comment,
            status="awaiting_manual_install", capabilities_json=None,
            expires_at=self._new_expiry(),
        )
        self._db.add(pending)
        await self._db.commit()
        logger.info("ssh_node_manual_provision", provision_id=str(provision_id), host=claims.host)
        return {
            "provision_id": provision_id,
            "public_key": line,
            "install_command_posix": posix_cmd,
            "install_command_windows": windows_cmd,
            "expires_at": pending.expires_at,
        }

    async def verify_provision(self, provision_id: uuid.UUID, owner_id: str) -> dict:
        pending = await self._get_pending(provision_id, owner_id)
        address = await check_host_allowed(pending.host)
        pem = decrypt_secret(pending.private_key_encrypted)

        def _verify() -> dict:
            t = _key_transport(address, pending.port, pending.username, pem,
                               pending.host_key_type, pending.host_key_b64)
            try:
                try:
                    _connect(t, key_auth=True)
                except KeyRejected as exc:
                    raise NodeServiceError(
                        "The node does not accept the key yet. Run the install command on the "
                        "worker as the same user, then verify again."
                    ) from exc
                timeout = self._settings.connect_timeout
                os_type = detect_os(t, timeout=timeout)
                adapter = select_adapter(os_type)
                _require_python(t, adapter, timeout)
                caps = _detect(t, adapter)
                if not caps.get("os_type"):
                    caps["os_type"] = os_type
                return caps
            finally:
                t.close()

        caps = await asyncio.to_thread(_verify)
        pending.status = "installed"
        pending.capabilities_json = json.dumps(caps)
        pending.expires_at = self._new_expiry()
        await self._db.commit()
        logger.info("ssh_node_manual_verified", provision_id=str(provision_id), host=pending.host)
        return self._provision_response(pending, caps)

    # -- 3. register --------------------------------------------------------------
    async def register(self, request: NodeRegisterRequest, owner_id: str) -> SSHNode:
        pending = await self._get_pending(request.provision_id, owner_id)
        if pending.status != "installed" or not pending.capabilities_json:
            raise InvalidOnboarding("The key has not been verified on the node yet")
        if await self._db.get(SSHNode, pending.id) is not None:
            raise NodeStateConflict("This provision was already registered")
        caps = json.loads(pending.capabilities_json)
        now = datetime.now(timezone.utc)
        node = SSHNode(
            id=pending.id,
            owner_id=owner_id,
            name=(request.name or "").strip() or pending.host,
            host=pending.host,
            port=pending.port,
            username=pending.username,
            auth_type=MANAGED_KEY,
            secret_encrypted=pending.private_key_encrypted,
            host_key_type=pending.host_key_type,
            host_key_b64=pending.host_key_b64,
            public_key=pending.public_key,
            key_comment=pending.key_comment,
            key_created_at=pending.created_at or now,
            health_status="online",
            last_health_check=now,
            status="online",
        )
        _apply_caps(node, caps)
        node.allocated_cpu, node.allocated_memory_gb = _clamp_allocation(
            request.allocated_cpu, request.allocated_memory_gb,
            node.detected_cpu, node.detected_memory_gb,
        )
        _clamp_gpu(node, request.allocated_gpu_count, request.allocated_gpu_memory_gb)
        self._db.add(node)
        await self._db.delete(pending)
        await self._db.commit()
        await self._db.refresh(node)
        logger.info("ssh_node_registered", node_id=str(node.id), host=node.host, os=node.os_type)
        return node

    # -- queries (owner-scoped) --------------------------------------------------------
    async def list_nodes(self, owner_id: str, skip: int = 0, limit: int = 100):
        total = await self._db.scalar(
            select(func.count()).select_from(SSHNode).where(SSHNode.owner_id == owner_id)
        )
        result = await self._db.execute(
            select(SSHNode)
            .where(SSHNode.owner_id == owner_id)
            .order_by(SSHNode.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all()), total or 0

    async def get_node(self, node_id: uuid.UUID, owner_id: str) -> Optional[SSHNode]:
        result = await self._db.execute(
            select(SSHNode).where(SSHNode.id == node_id, SSHNode.owner_id == owner_id)
        )
        return result.scalar_one_or_none()

    async def _require_node(self, node_id: uuid.UUID, owner_id: str) -> SSHNode:
        node = await self.get_node(node_id, owner_id)
        if node is None:
            raise NodeNotFound("Node not found")
        return node

    async def update_node(self, node_id: uuid.UUID, owner_id: str, request: NodeUpdateRequest) -> SSHNode:
        node = await self._require_node(node_id, owner_id)
        if request.name is not None and request.name.strip():
            node.name = request.name.strip()
        if request.status is not None:
            node.status = request.status
        cpu = request.allocated_cpu if request.allocated_cpu is not None else node.allocated_cpu
        mem = request.allocated_memory_gb if request.allocated_memory_gb is not None else node.allocated_memory_gb
        node.allocated_cpu, node.allocated_memory_gb = _clamp_allocation(
            cpu, mem, node.detected_cpu, node.detected_memory_gb
        )
        _clamp_gpu(node, request.allocated_gpu_count, request.allocated_gpu_memory_gb)
        if request.env_cache_gb is not None:
            node.env_cache_gb = float(request.env_cache_gb)
        await self._db.commit()
        await self._db.refresh(node)
        return node

    # -- delete: remove our key line first -------------------------------------------
    async def delete_node(self, node_id: uuid.UUID, owner_id: str) -> dict:
        node = await self._require_node(node_id, owner_id)
        key_removed = True
        message = "Node removed."
        if node.key_comment:
            def _remove() -> int:
                t = transport_for_node(node)
                try:
                    _connect(t, key_auth=node.auth_type != "password")
                    os_type = node.os_type or detect_os(t, timeout=self._settings.connect_timeout)
                    if os_type != "windows":
                        _remove_worker_cache(t)
                    return remove_authorized_key(t, os_type, node.key_comment, timeout=_KEY_OP_TIMEOUT)
                finally:
                    t.close()

            try:
                removed = await asyncio.to_thread(_remove)
                message = f"Node removed; {removed} platform key line(s) deleted from the worker."
            except Exception as exc:
                key_removed = False
                message = (
                    "Node removed, but the worker could not be reached to delete the platform key. "
                    f"Remove the line ending in '{node.key_comment}' from its authorized_keys."
                )
                logger.warning("ssh_node_key_removal_failed", node_id=str(node_id), error=str(exc))
        await self._db.delete(node)
        await self._db.commit()
        logger.info("ssh_node_deleted", node_id=str(node_id), key_removed=key_removed)
        return {"deleted": True, "key_removed": key_removed, "message": message}

    # -- health / refresh ----------------------------------------------------------------
    async def health_check(self, node_id: uuid.UUID, owner_id: str) -> SSHNode:
        node = await self._require_node(node_id, owner_id)

        def _probe() -> bool:
            t = transport_for_node(node)
            try:
                return t.test_connection().get("status") == "success"
            except Exception:
                return False
            finally:
                t.close()

        ok = await asyncio.to_thread(_probe)
        node.health_status = "online" if ok else "unreachable"
        node.last_health_check = datetime.now(timezone.utc)
        await self._db.commit()
        await self._db.refresh(node)
        logger.info("ssh_node_health", node_id=str(node_id), health=node.health_status)
        return node

    async def refresh(self, node_id: uuid.UUID, owner_id: str) -> SSHNode:
        node = await self._require_node(node_id, owner_id)

        def _redetect() -> dict:
            t = transport_for_node(node)
            try:
                _connect(t, key_auth=node.auth_type != "password")
                os_type = detect_os(t, timeout=self._settings.connect_timeout)
                caps = _detect(t, select_adapter(os_type))
                if not caps.get("os_type"):
                    caps["os_type"] = os_type
                return caps
            finally:
                t.close()

        try:
            caps = await asyncio.to_thread(_redetect)
        except NodeUnreachable:
            node.health_status = "unreachable"
            node.last_health_check = datetime.now(timezone.utc)
            await self._db.commit()
            raise
        _apply_caps(node, caps)
        node.allocated_cpu, node.allocated_memory_gb = _clamp_allocation(
            node.allocated_cpu, node.allocated_memory_gb, node.detected_cpu, node.detected_memory_gb
        )
        _clamp_gpu(node)
        node.health_status = "online"
        node.last_health_check = datetime.now(timezone.utc)
        await self._db.commit()
        await self._db.refresh(node)
        logger.info("ssh_node_refreshed", node_id=str(node_id), runtimes=sorted(caps.get("runtimes") or {}))
        return node

    # -- rotate-key: install new, verify, then remove old ----------------------------
    async def rotate_key(self, node_id: uuid.UUID, owner_id: str) -> SSHNode:
        node = await self._require_node(node_id, owner_id)
        if node.auth_type != MANAGED_KEY:
            raise NodeStateConflict("This node uses a legacy credential; re-secure it first")
        comment = node.key_comment or key_comment(node.id)
        old_blob = node.public_key.split()[1] if node.public_key and len(node.public_key.split()) > 1 else None
        os_hint = node.os_type

        def _install_new() -> tuple[ManagedKey, str]:
            old_t = transport_for_node(node)
            _connect(old_t, key_auth=True)
            try:
                os_type = os_hint or detect_os(old_t, timeout=self._settings.connect_timeout)
                new = generate_keypair()
                install_authorized_key(old_t, os_type, authorized_key_line(new.public_key, comment),
                                       comment, timeout=_KEY_OP_TIMEOUT)
                new_t = _key_transport(node.host, node.port, node.username, new.private_key_pem,
                                       node.host_key_type, node.host_key_b64)
                try:
                    _connect(new_t, key_auth=True)
                    if new_t.exec("echo dp-ok", timeout=self._settings.connect_timeout).get("exit_code") != 0:
                        raise NodeServiceError("The new key logged in but could not run a command")
                except Exception:
                    try:
                        remove_authorized_key(old_t, os_type, comment, new.key_blob, timeout=_KEY_OP_TIMEOUT)
                    except Exception as exc:
                        logger.warning("ssh_key_rollback_failed", node_id=str(node_id), error=str(exc))
                    raise
                finally:
                    new_t.close()
                return new, os_type
            finally:
                old_t.close()

        new, os_type = await asyncio.to_thread(_install_new)
        old_transport = transport_for_node(node)  # built before the secret is replaced
        try:
            node.secret_encrypted = encrypt_secret(new.private_key_pem)
            node.public_key = new.public_key
            node.key_comment = comment
            node.key_created_at = datetime.now(timezone.utc)
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            await asyncio.to_thread(_best_effort_remove, old_transport, os_type, comment, new.key_blob, key_auth=True)
            raise
        if old_blob:
            new_t = _key_transport(node.host, node.port, node.username, new.private_key_pem,
                                   node.host_key_type, node.host_key_b64)
            if not await asyncio.to_thread(_best_effort_remove, new_t, os_type, comment, old_blob, key_auth=True):
                logger.warning("ssh_old_key_line_left", node_id=str(node_id))
        await self._db.refresh(node)
        logger.info("ssh_node_key_rotated", node_id=str(node_id), key_fingerprint=new.fingerprint)
        return node

    # -- resecure: legacy password/private_key node -> managed key --------------------
    async def resecure(self, node_id: uuid.UUID, owner_id: str, request: NodeResecureRequest) -> SSHNode:
        node = await self._require_node(node_id, owner_id)
        if node.auth_type not in LEGACY_AUTH_TYPES:
            raise NodeStateConflict("This node already uses a platform-managed key")
        address = await check_host_allowed(node.host)
        if request.password:
            password, private_key = request.password, None
        else:
            stored = decrypt_secret(node.secret_encrypted)
            password = stored if node.auth_type == "password" else None
            private_key = stored if node.auth_type == "private_key" else None
        cred = SSHTransport(
            host=address, port=node.port, username=node.username,
            password=password, private_key=private_key,
            host_key_type=node.host_key_type, host_key_b64=node.host_key_b64,
            connect_timeout=self._settings.connect_timeout,
        )
        comment = key_comment(node.id)
        key, caps, host_key = await asyncio.to_thread(
            _install_and_verify, cred, comment=comment, require_python=False,
            cred_is_key=private_key is not None,
        )
        try:
            node.auth_type = MANAGED_KEY
            # Overwrites (wipes) the legacy password / private key ciphertext.
            node.secret_encrypted = encrypt_secret(key.private_key_pem)
            node.public_key = key.public_key
            node.key_comment = comment
            node.key_created_at = datetime.now(timezone.utc)
            if not node.host_key_b64:
                node.host_key_type = host_key["host_key_type"]
                node.host_key_b64 = host_key["host_key_b64"]
            _apply_caps(node, caps)
            node.allocated_cpu, node.allocated_memory_gb = _clamp_allocation(
                node.allocated_cpu, node.allocated_memory_gb, node.detected_cpu, node.detected_memory_gb
            )
            _clamp_gpu(node)
            node.health_status = "online"
            node.last_health_check = datetime.now(timezone.utc)
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            await asyncio.to_thread(
                _best_effort_remove,
                _key_transport(address, node.port, node.username, key.private_key_pem,
                               host_key["host_key_type"], host_key["host_key_b64"]),
                caps.get("os_type") or "linux", comment, None, key_auth=True,
            )
            raise
        del cred, password, private_key
        await self._db.refresh(node)
        logger.info("ssh_node_resecured", node_id=str(node_id), key_fingerprint=key.fingerprint)
        return node


# --- recovery sweep ------------------------------------------------------------------
def _remove_pending_key(pending: PendingProvision) -> int:
    caps = json.loads(pending.capabilities_json) if pending.capabilities_json else {}
    t = _key_transport(pending.host, pending.port, pending.username,
                       decrypt_secret(pending.private_key_encrypted),
                       pending.host_key_type, pending.host_key_b64)
    try:
        _connect(t, key_auth=True)
        os_type = caps.get("os_type") or detect_os(t, timeout=get_settings().connect_timeout)
        return remove_authorized_key(t, os_type, pending.key_comment, timeout=_KEY_OP_TIMEOUT)
    finally:
        t.close()


async def cleanup_expired_provisions() -> int:
    """Delete expired ``pending_provisions`` rows, first removing their key
    line from the node when it is reachable. Returns the number of rows deleted.

    An unreachable node keeps the line, but the private key is gone with the
    row, so the leftover public key grants nobody access.
    """
    now = datetime.now(timezone.utc)
    async with get_db_manager().session() as session:
        rows = list(
            (await session.execute(
                select(PendingProvision).where(PendingProvision.expires_at < now)
            )).scalars().all()
        )
        for pending in rows:
            try:
                removed = await asyncio.to_thread(_remove_pending_key, pending)
                logger.info("ssh_pending_key_removed", provision_id=str(pending.id), removed=removed)
            except KeyRejected:
                pass  # never installed (manual flow not completed) -> nothing to remove
            except Exception as exc:
                logger.warning("ssh_pending_key_cleanup_failed", provision_id=str(pending.id), error=str(exc))
            await session.delete(pending)
    if rows:
        logger.info("ssh_pending_provisions_expired", count=len(rows))
    return len(rows)
