"""ssh — Unified SSH transport, executor, key management, and authorized_keys installation.

Consolidated module combining:
- SSHTransport & SFTP transfers (formerly ssh_transport.py)
- SSHExecutor legacy task execution & resource detection (formerly ssh_client.py)
- Ed25519 managed key pairs & onboarding token validation (formerly key_manager.py)
- Cross-platform authorized_keys installation and removal (formerly key_install.py)
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import re
import shlex
import socket
import time
from typing import Any, Callable, Dict, Optional, Tuple
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import jwt
from os_adapters import OSAdapter, get_adapter
import paramiko
from runtimes import ResourceSpec
import structlog

from config import get_settings

logger = structlog.get_logger(__name__)

# ==============================================================================
# 1. CORE SSH CONNECTION & HOST KEY PINNING
# ==============================================================================

_KEY_CLASSES = (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey)


def _host_key_name(host: str, port: int) -> str:
    """Reproduce paramiko's internal known-hosts key name for a host:port."""
    return host if port == 22 else f"[{host}]:{port}"


def fingerprint(host_key_type: str, host_key_b64: str) -> str:
    """Return an OpenSSH-style SHA256:... fingerprint for a pinned key."""
    raw = base64.b64decode(host_key_b64)
    digest = hashlib.sha256(raw).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class HostKeyMismatch(Exception):
    """Raised when a node presents a host key other than the pinned one."""


class SSHTransport:
    """One SSH connection to one node. Open with connect() (or use as a context manager)."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: Optional[str] = None,
        private_key: Optional[str] = None,
        host_key_type: Optional[str] = None,
        host_key_b64: Optional[str] = None,
        connect_timeout: int = 10,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.private_key = private_key
        self.host_key_type = host_key_type
        self.host_key_b64 = host_key_b64
        self.connect_timeout = connect_timeout
        self._client: Optional[paramiko.SSHClient] = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    def _load_pkey(self) -> paramiko.PKey:
        last_err: Optional[Exception] = None
        for key_cls in _KEY_CLASSES:
            try:
                return key_cls.from_private_key(io.StringIO(self.private_key))
            except paramiko.SSHException as exc:
                last_err = exc
        raise ValueError(f"Unsupported or invalid private key: {last_err}")

    def connect(self) -> Dict[str, str]:
        client = paramiko.SSHClient()
        if self.host_key_b64 and self.host_key_type:
            pinned = paramiko.PKey.from_type_string(
                self.host_key_type, base64.b64decode(self.host_key_b64)
            )
            client.get_host_keys().add(
                _host_key_name(self.host, self.port), self.host_key_type, pinned
            )
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: Dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": self.connect_timeout,
            "banner_timeout": self.connect_timeout,
            "auth_timeout": self.connect_timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if self.private_key:
            connect_kwargs["pkey"] = self._load_pkey()
        elif self.password:
            connect_kwargs["password"] = self.password
        else:
            raise ValueError("Either password or private_key must be provided")

        try:
            client.connect(**connect_kwargs)
        except paramiko.SSHException as exc:
            if "not found in known_hosts" in str(exc) or "Host key" in str(exc):
                raise HostKeyMismatch(
                    "Host key does not match the pinned key for this node"
                ) from exc
            raise
        self._client = client
        return self.capture_host_key()

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            finally:
                self._client = None

    def __enter__(self) -> "SSHTransport":
        if not self.is_connected:
            self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _require_client(self) -> paramiko.SSHClient:
        if self._client is None:
            raise RuntimeError("SSHTransport is not connected; call connect() first")
        return self._client

    def capture_host_key(self) -> Dict[str, str]:
        remote = self._require_client().get_transport().get_remote_server_key()
        key_type = remote.get_name()
        key_b64 = remote.get_base64()
        return {
            "host_key_type": key_type,
            "host_key_b64": key_b64,
            "fingerprint": fingerprint(key_type, key_b64),
        }

    def exec(
        self,
        command: str,
        input_data: str = "",
        timeout: Optional[int] = None,
        max_output_bytes: int = 1_000_000,
    ) -> Dict[str, Any]:
        client = self._require_client()
        chan_timeout = timeout if timeout is not None else self.connect_timeout
        stdin, stdout, stderr = client.exec_command(command, timeout=chan_timeout)
        if input_data:
            try:
                stdin.write(input_data)
            except OSError:
                pass
        stdin.channel.shutdown_write()
        out = stdout.read(max_output_bytes).decode(errors="replace")
        err = stderr.read(max_output_bytes).decode(errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return {"exit_code": exit_code, "stdout": out, "stderr": err}

    def exec_stream(
        self,
        command: str,
        input_data: str = "",
        timeout: Optional[int] = None,
        on_line: Optional[Callable[[str], None]] = None,
        max_output_bytes: int = 1_000_000,
    ) -> Dict[str, Any]:
        client = self._require_client()
        chan_timeout = timeout if timeout is not None else self.connect_timeout
        stdin, stdout, stderr = client.exec_command(command, timeout=chan_timeout)
        if input_data:
            try:
                stdin.write(input_data)
            except OSError:
                pass
        stdin.channel.shutdown_write()
        collected: list[str] = []
        total = 0
        for raw in iter(stdout.readline, ""):
            total += len(raw)
            if total > max_output_bytes:
                break
            collected.append(raw)
            if on_line:
                on_line(raw.rstrip("\n"))
        err = stderr.read(max_output_bytes).decode(errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return {"exit_code": exit_code, "stdout": "".join(collected), "stderr": err}

    def put_text(self, remote_path: str, content: str) -> None:
        sftp = self._require_client().open_sftp()
        try:
            with sftp.file(remote_path, "w") as fh:
                fh.write(content)
        finally:
            sftp.close()

    def upload(self, local_path: str, remote_path: str) -> None:
        sftp = self._require_client().open_sftp()
        try:
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()

    def fetch(self, remote_path: str, local_path: str) -> None:
        """Copy a remote file to ``local_path`` (streamed, for large files)."""
        sftp = self._require_client().open_sftp()
        try:
            sftp.get(remote_path, local_path)
        finally:
            sftp.close()

    def download(self, remote_path: str) -> bytes:
        sftp = self._require_client().open_sftp()
        try:
            with sftp.file(remote_path, "r") as fh:
                return fh.read()
        finally:
            sftp.close()

    def remove(self, remote_path: str) -> None:
        try:
            sftp = self._require_client().open_sftp()
            try:
                sftp.remove(remote_path)
            finally:
                sftp.close()
        except Exception:
            pass

    def test_connection(self) -> Dict[str, Any]:
        own = not self.is_connected
        try:
            if own:
                self.connect()
            res = self.exec("echo connection_successful", timeout=self.connect_timeout)
            if res["exit_code"] != 0:
                return {"status": "error", "message": "Command execution failed"}
            result = {"status": "success", "message": "Successfully connected via SSH"}
            result.update(self.capture_host_key())
            return result
        except Exception as exc:
            logger.error("ssh_transport_test_failed", host=self.host, error=str(exc))
            return {"status": "error", "message": str(exc)}
        finally:
            if own:
                self.close()


# ==============================================================================
# 2. LEGACY SSHEXECUTOR (TASK EXECUTION & AUTO-DETECTION)
# ==============================================================================

class SSHExecutor:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: Optional[str] = None,
        private_key: Optional[str] = None,
        host_key_type: Optional[str] = None,
        host_key_b64: Optional[str] = None,
        connect_timeout: int = 10,
        os_type: str = "linux",
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.private_key = private_key
        self.host_key_type = host_key_type
        self.host_key_b64 = host_key_b64
        self.connect_timeout = connect_timeout
        self.os_type = os_type
        self._adapter: OSAdapter = get_adapter(os_type)

    def _load_pkey(self) -> paramiko.PKey:
        last_err: Optional[Exception] = None
        for key_cls in _KEY_CLASSES:
            try:
                return key_cls.from_private_key(io.StringIO(self.private_key))
            except paramiko.SSHException as exc:
                last_err = exc
        raise ValueError(f"Unsupported or invalid private key: {last_err}")

    def _get_client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        if self.host_key_b64 and self.host_key_type:
            pinned = paramiko.PKey.from_type_string(
                self.host_key_type, base64.b64decode(self.host_key_b64)
            )
            client.get_host_keys().add(
                _host_key_name(self.host, self.port), self.host_key_type, pinned
            )
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: Dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": self.connect_timeout,
            "banner_timeout": self.connect_timeout,
            "auth_timeout": self.connect_timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if self.private_key:
            connect_kwargs["pkey"] = self._load_pkey()
        elif self.password:
            connect_kwargs["password"] = self.password
        else:
            raise ValueError("Either password or private_key must be provided")

        try:
            client.connect(**connect_kwargs)
        except paramiko.SSHException as exc:
            if "not found in known_hosts" in str(exc) or "Host key" in str(exc):
                raise HostKeyMismatch(
                    "Host key does not match the pinned key for this node"
                ) from exc
            raise
        return client

    def _capture_host_key(self, client: paramiko.SSHClient) -> Dict[str, str]:
        remote = client.get_transport().get_remote_server_key()
        key_type = remote.get_name()
        key_b64 = remote.get_base64()
        return {
            "host_key_type": key_type,
            "host_key_b64": key_b64,
            "fingerprint": fingerprint(key_type, key_b64),
        }

    def test_connection(self) -> Dict[str, Any]:
        client = None
        try:
            client = self._get_client()
            _, stdout, _ = client.exec_command("echo connection_successful", timeout=self.connect_timeout)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                return {"status": "error", "message": "Command execution failed"}
            result = {"status": "success", "message": "Successfully connected via SSH"}
            result.update(self._capture_host_key(client))
            return result
        except Exception as exc:
            logger.error("ssh_test_failed", host=self.host, error=str(exc))
            return {"status": "error", "message": str(exc)}
        finally:
            if client:
                client.close()

    def detect_resources(self) -> Dict[str, Any]:
        client = None
        try:
            client = self._get_client()

            def run(cmd: str) -> str:
                _, out, _ = client.exec_command(cmd, timeout=self.connect_timeout)
                return out.read().decode(errors="replace").strip()

            cpu_raw = run("nproc --all 2>/dev/null || grep -c ^processor /proc/cpuinfo")
            mem_kb_raw = run("awk '/^MemTotal:/{print $2}' /proc/meminfo")
            gpu_raw = run("nvidia-smi -L 2>/dev/null | wc -l")
            os_info = run("(. /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\") || uname -sr")

            cpu = int(cpu_raw or 0)
            memory_gb = round(int(mem_kb_raw or 0) / (1024 * 1024), 2)
            gpu = int(gpu_raw or 0)

            result = {
                "status": "success",
                "detected_cpu": cpu,
                "detected_memory_gb": memory_gb,
                "detected_gpu": gpu,
                "os_info": os_info or None,
            }
            result.update(self._capture_host_key(client))
            return result
        except Exception as exc:
            logger.error("ssh_detect_failed", host=self.host, error=str(exc))
            return {"status": "error", "message": str(exc)}
        finally:
            if client:
                client.close()

    def run_task(
        self,
        code: str,
        input_data: str = "",
        allocated_cpu: int = 1,
        nice: int = 19,
        timeout_seconds: int = 120,
        max_output_bytes: int = 1_000_000,
    ) -> Dict[str, Any]:
        client = None
        remote_path = f"/tmp/task_{uuid.uuid4().hex}.py"
        try:
            client = self._get_client()
            sftp = client.open_sftp()
            try:
                with sftp.file(remote_path, "w") as fh:
                    fh.write(code)
            finally:
                sftp.close()

            cmd = self._adapter.build_execute_command(
                remote_path,
                ResourceSpec(
                    cpu_cores=max(1, allocated_cpu),
                    memory_gb=0.0,
                    timeout_seconds=timeout_seconds,
                ),
                nice=nice,
            )

            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout_seconds + 15)
            if input_data:
                stdin.write(input_data)
            stdin.channel.shutdown_write()

            out = stdout.read(max_output_bytes).decode(errors="replace")
            err = stderr.read(max_output_bytes).decode(errors="replace")
            exit_status = stdout.channel.recv_exit_status()

            status = "success" if exit_status == 0 else "error"
            if exit_status == 124:
                err = (err + "\n[task timed out]").strip()
            return {
                "status": status,
                "exit_code": exit_status,
                "stdout": out,
                "stderr": err,
            }
        except Exception as exc:
            logger.error("ssh_task_failed", host=self.host, error=str(exc))
            return {"status": "error", "exit_code": None, "stdout": "", "stderr": str(exc)}
        finally:
            if client:
                try:
                    cleanup = client.open_sftp()
                    try:
                        cleanup.remove(remote_path)
                    finally:
                        cleanup.close()
                except Exception:
                    pass
                client.close()


# ==============================================================================
# 3. KEY MANAGER (ED25519 KEYS & SIGNED ONBOARDING TOKEN)
# ==============================================================================

ONBOARDING_AUDIENCE = "dp-platform:node-onboarding"
ONBOARDING_TYPE = "node_onboarding"
ONBOARDING_ISSUER = "ssh-executor"

_COMMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}:[A-Za-z0-9-]{1,64}$")
_OPTIONS_RE = re.compile(r'^[A-Za-z0-9,=".:/@_*+-]*$')
_PUBKEY_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|ssh-rsa) [A-Za-z0-9+/]+={0,3}$")


class InvalidOnboardingToken(ValueError):
    """The onboarding token is malformed, tampered with, expired or not ours."""


class HostKeyFetchError(Exception):
    """The node could not be reached or did not complete an SSH handshake."""


@dataclass(frozen=True)
class ManagedKey:
    private_key_pem: str
    public_key: str
    fingerprint: str

    @property
    def key_type(self) -> str:
        return self.public_key.split()[0]

    @property
    def key_blob(self) -> str:
        return self.public_key.split()[1]


def generate_keypair() -> ManagedKey:
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return ManagedKey(private_key_pem=pem, public_key=public, fingerprint=public_key_fingerprint(public))


def public_key_fingerprint(public_key: str) -> str:
    parts = public_key.split()
    if len(parts) < 2:
        raise ValueError("Not an OpenSSH public key")
    return fingerprint(parts[0], parts[1])


def key_comment(key_id: uuid.UUID | str) -> str:
    comment = f"{get_settings().managed_key_comment_prefix}:{key_id}"
    if not _COMMENT_RE.match(comment):
        raise ValueError("managed_key_comment_prefix contains unsupported characters")
    return comment


def validate_comment(comment: str) -> str:
    if not _COMMENT_RE.match(comment or ""):
        raise ValueError(f"Invalid managed key comment: {comment!r}")
    return comment


def authorized_key_line(public_key: str, comment: str) -> str:
    public_key = public_key.strip()
    if not _PUBKEY_RE.match(public_key):
        raise ValueError("Unsupported public key format")
    validate_comment(comment)
    options = (get_settings().managed_key_options or "").strip()
    if not _OPTIONS_RE.match(options):
        raise ValueError("managed_key_options contains unsupported characters")
    return f"{options} {public_key} {comment}" if options else f"{public_key} {comment}"


def fetch_host_key(host: str, port: int, timeout: Optional[int] = None) -> dict:
    timeout = timeout or get_settings().connect_timeout
    sock = None
    transport = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = timeout
        transport.handshake_timeout = timeout
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        key_type = key.get_name()
        key_b64 = key.get_base64()
        return {
            "host_key_type": key_type,
            "host_key_b64": key_b64,
            "fingerprint": fingerprint(key_type, key_b64),
        }
    except (OSError, paramiko.SSHException, EOFError) as exc:
        raise HostKeyFetchError(f"Could not reach an SSH server at {host}:{port}: {exc}") from exc
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


@dataclass(frozen=True)
class OnboardingClaims:
    owner_id: str
    host: str
    port: int
    username: str
    host_key_type: str
    host_key_b64: str
    expires_at: datetime

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.host_key_type, self.host_key_b64)


def issue_onboarding_token(
    *,
    owner_id: str,
    host: str,
    port: int,
    username: str,
    host_key_type: str,
    host_key_b64: str,
) -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "iss": ONBOARDING_ISSUER,
        "aud": ONBOARDING_AUDIENCE,
        "typ": ONBOARDING_TYPE,
        "sub": owner_id,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + settings.onboarding_token_ttl_seconds,
        "host": host,
        "port": int(port),
        "username": username,
        "hkt": host_key_type,
        "hkb": host_key_b64,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_onboarding_token(token: str, owner_id: str) -> OnboardingClaims:
    settings = get_settings()
    try:
        data = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=ONBOARDING_AUDIENCE,
            issuer=ONBOARDING_ISSUER,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidOnboardingToken("Onboarding token expired; connect to the node again") from exc
    except jwt.PyJWTError as exc:
        raise InvalidOnboardingToken("Invalid onboarding token") from exc

    if data.get("typ") != ONBOARDING_TYPE:
        raise InvalidOnboardingToken("Invalid onboarding token")
    if data.get("sub") != owner_id:
        raise InvalidOnboardingToken("Onboarding token belongs to another user")
    try:
        claims = OnboardingClaims(
            owner_id=str(data["sub"]),
            host=str(data["host"]),
            port=int(data["port"]),
            username=str(data["username"]),
            host_key_type=str(data["hkt"]),
            host_key_b64=str(data["hkb"]),
            expires_at=datetime.fromtimestamp(int(data["exp"]), tz=timezone.utc),
        )
        base64.b64decode(claims.host_key_b64, validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidOnboardingToken("Invalid onboarding token") from exc
    return claims


# ==============================================================================
# 4. KEY INSTALLATION & REMOVAL
# ==============================================================================

_MARKER = "DPKEY OK"
_LINE_RE = re.compile(r'^[A-Za-z0-9 ,=".:/@_*+-]{16,4096}$')
_BLOB_RE = re.compile(r"^[A-Za-z0-9+/]*={0,3}$")
_HEREDOC_TAG = "__DP_PLATFORM_KEY_LINE__"


class KeyInstallError(RuntimeError):
    """The remote install/remove script failed."""


def _check_line(line: str, comment: str) -> str:
    if not _LINE_RE.match(line or "") or "\n" in line or "\r" in line:
        raise ValueError("Refusing to install a malformed authorized_keys line")
    if line.split()[-1] != comment:
        raise ValueError("authorized_keys line must end with its comment")
    return line


def _check_blob(blob: Optional[str]) -> str:
    blob = blob or ""
    if not _BLOB_RE.match(blob):
        raise ValueError("Invalid public key blob")
    return blob


def key_blob_of(line: str) -> str:
    parts = line.split()
    for i, part in enumerate(parts[:-1]):
        if part.startswith(("ssh-", "ecdsa-")):
            return parts[i + 1]
    raise ValueError("No key type found in authorized_keys line")


_AWK_MATCH = (
    '{ m = ($NF == c); if (m && k != "") { m = 0; '
    "for (i = 1; i < NF; i++) if ($i == k) m = 1 } }"
)


def posix_install_script(line: str, comment: str) -> str:
    validate_comment(comment)
    _check_line(line, comment)
    blob = _check_blob(key_blob_of(line))
    return f"""set -u
umask 077
line=$(cat <<'{_HEREDOC_TAG}'
{line}
{_HEREDOC_TAG}
)
comment={shlex.quote(comment)}
blob={shlex.quote(blob)}
d="$HOME/.ssh"
f="$d/authorized_keys"
mkdir -p "$d" || exit 10
touch "$f" || exit 11
n=$(awk -v c="$comment" -v k="$blob" '{_AWK_MATCH} m == 1 {{ n++ }} END {{ print n + 0 }}' "$f") || exit 12
if [ "$n" -gt 0 ]; then
  state=present
else
  if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then printf '\\n' >> "$f" || exit 13; fi
  printf '%s\\n' "$line" >> "$f" || exit 13
  state=added
fi
chmod 700 "$d" || exit 14
chmod 600 "$f" || exit 14
if command -v restorecon >/dev/null 2>&1; then restorecon -R "$d" >/dev/null 2>&1 || true; fi
echo "{_MARKER} state=$state path=$f"
"""


def posix_remove_script(comment: str, key_blob: Optional[str] = None) -> str:
    validate_comment(comment)
    blob = _check_blob(key_blob)
    return f"""set -u
umask 077
comment={shlex.quote(comment)}
blob={shlex.quote(blob)}
f="$HOME/.ssh/authorized_keys"
if [ ! -f "$f" ]; then echo "{_MARKER} removed=0 path=$f"; exit 0; fi
n=$(awk -v c="$comment" -v k="$blob" '{_AWK_MATCH} m == 1 {{ n++ }} END {{ print n + 0 }}' "$f") || exit 20
if [ "$n" -gt 0 ]; then
  tmp="$f.dp-tmp.$$"
  cp -p "$f" "$tmp" || exit 21
  if ! awk -v c="$comment" -v k="$blob" '{_AWK_MATCH} m == 0 {{ print }}' "$f" > "$tmp"; then rm -f "$tmp"; exit 22; fi
  if [ -L "$f" ]; then
    cat "$tmp" > "$f" || {{ rm -f "$tmp"; exit 23; }}
    rm -f "$tmp"
  else
    mv -f "$tmp" "$f" || {{ rm -f "$tmp"; exit 23; }}
  fi
fi
echo "{_MARKER} removed=$n path=$f"
"""


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_PS_TARGET = r"""
$isAdmin = $false
try {
  $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch {}
if (-not $isAdmin) {
  try { if ((whoami.exe /groups) -match 'S-1-5-32-544') { $isAdmin = $true } } catch {}
}
$useAdminFile = $false
if ($isAdmin) {
  $useAdminFile = $true
  $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
  if (Test-Path -LiteralPath $cfg) {
    $useAdminFile = [bool](Select-String -LiteralPath $cfg -Pattern '^\s*AuthorizedKeysFile\s+.*administrators_authorized_keys' -Quiet)
  }
}
if ($useAdminFile) {
  $dir = Join-Path $env:ProgramData 'ssh'
  $file = Join-Path $dir 'administrators_authorized_keys'
} else {
  $dir = Join-Path $env:USERPROFILE '.ssh'
  $file = Join-Path $dir 'authorized_keys'
}
"""

_PS_MATCH_FN = r"""
function Test-DpLine([string]$l) {
  $parts = @($l.Trim() -split '\s+')
  if ($parts.Count -lt 2 -or -not ($parts[-1] -ceq $comment)) { return $false }
  if ($blob -ne '') { return ($parts -ccontains $blob) }
  return $true
}
"""


def windows_install_script(line: str, comment: str) -> str:
    validate_comment(comment)
    _check_line(line, comment)
    blob = _check_blob(key_blob_of(line))
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$line = {_ps_literal(line)}\n"
        f"$comment = {_ps_literal(comment)}\n"
        f"$blob = {_ps_literal(blob)}\n"
        + _PS_MATCH_FN
        + _PS_TARGET
        + r"""
if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$text = ''
if (Test-Path -LiteralPath $file) { $text = [IO.File]::ReadAllText($file) }
$present = $false
foreach ($l in ($text -split "`r?`n")) { if (Test-DpLine $l) { $present = $true } }
$state = 'present'
if (-not $present) {
  $prefix = ''
  if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { $prefix = "`r`n" }
  [IO.File]::AppendAllText($file, $prefix + $line + "`r`n", (New-Object Text.UTF8Encoding($false)))
  $state = 'added'
}
if ($useAdminFile) {
  & icacls.exe $file /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "icacls failed with exit code $LASTEXITCODE" }
}
Write-Output "DPKEY OK state=$state path=$file"
"""
    )


def windows_remove_script(comment: str, key_blob: Optional[str] = None) -> str:
    validate_comment(comment)
    blob = _check_blob(key_blob)
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$comment = {_ps_literal(comment)}\n"
        f"$blob = {_ps_literal(blob)}\n"
        + _PS_MATCH_FN
        + r"""
$total = 0
$candidates = @((Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'), (Join-Path $env:USERPROFILE '.ssh\authorized_keys'))
foreach ($file in $candidates) {
  try {
    if (-not (Test-Path -LiteralPath $file)) { continue }
    $keep = New-Object System.Collections.Generic.List[string]
    $n = 0
    foreach ($l in [IO.File]::ReadAllLines($file)) { if (Test-DpLine $l) { $n++ } else { $keep.Add($l) } }
    if ($n -gt 0) {
      $body = ($keep -join "`r`n")
      if ($keep.Count -gt 0) { $body += "`r`n" }
      [IO.File]::WriteAllText($file, $body, (New-Object Text.UTF8Encoding($false)))
      $total += $n
    }
  } catch [System.UnauthorizedAccessException] {}
}
Write-Output "DPKEY OK removed=$total"
"""
    )


def powershell_encoded_command(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"


def _run(transport: SSHTransport, os_type: str, script: str, timeout: int) -> str:
    if (os_type or "").lower() == "windows":
        res = transport.exec(powershell_encoded_command(script), timeout=timeout)
    else:
        res = transport.exec("sh -s", input_data=script, timeout=timeout)
    stdout = res.get("stdout") or ""
    marker = next((l.strip() for l in stdout.splitlines() if l.strip().startswith(_MARKER)), None)
    if res.get("exit_code") != 0 or marker is None:
        err = (res.get("stderr") or "").strip()[-500:]
        raise KeyInstallError(
            f"authorized_keys update failed (exit {res.get('exit_code')}): {err or stdout.strip()[-500:]}"
        )
    return marker


def _is_windows(os_type: str) -> bool:
    return (os_type or "").lower() == "windows"


def _field(marker: str, name: str) -> Optional[str]:
    m = re.search(rf"\b{name}=(\S.*?)(?=\s+\w+=|$)", marker)
    return m.group(1) if m else None


def install_authorized_key(transport: SSHTransport, os_type: str, line: str, comment: str, timeout: int = 30) -> dict:
    script = (windows_install_script if _is_windows(os_type) else posix_install_script)(line, comment)
    marker = _run(transport, os_type, script, timeout)
    return {"state": _field(marker, "state"), "path": _field(marker, "path")}


def remove_authorized_key(
    transport: SSHTransport, os_type: str, comment: str, key_blob: Optional[str] = None, timeout: int = 30
) -> int:
    script = (windows_remove_script if _is_windows(os_type) else posix_remove_script)(comment, key_blob)
    marker = _run(transport, os_type, script, timeout)
    try:
        return int(_field(marker, "removed") or 0)
    except ValueError:
        return 0


def manual_install_commands(line: str, comment: str) -> Tuple[str, str]:
    validate_comment(comment)
    _check_line(line, comment)
    posix_body = (
        'umask 077; mkdir -p "$HOME/.ssh" && touch "$HOME/.ssh/authorized_keys"'
        ' && chmod 700 "$HOME/.ssh" && chmod 600 "$HOME/.ssh/authorized_keys"'
        ' && { grep -qF -- "$2" "$HOME/.ssh/authorized_keys"'
        ' || printf "\\n%s\\n" "$1" >> "$HOME/.ssh/authorized_keys"; }'
    )
    posix = f"sh -c {shlex.quote(posix_body)} sh {shlex.quote(line)} {shlex.quote(comment)}"
    windows = (
        "# Run in PowerShell as the SSH user (elevated if that user is an administrator)\n"
        + windows_install_script(line, comment)
    )
    return posix, windows
