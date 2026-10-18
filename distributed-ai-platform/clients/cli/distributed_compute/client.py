"""A small client for the extension gateway (the API the VS Code extension uses)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

DEFAULT_MASTER = "http://localhost:8090"


class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, master: Optional[str] = None, token: Optional[str] = None, timeout: float = 60.0):
        self.master = (master or os.environ.get("DC_MASTER") or DEFAULT_MASTER).rstrip("/")
        self.token = token or os.environ.get("DC_TOKEN") or None
        self.timeout = timeout

    # -- transport -------------------------------------------------------------

    def _request(self, method: str, path: str, *, body: Any = None, raw: Optional[bytes] = None,
                 content_type: Optional[str] = None, timeout: Optional[float] = None, auth: bool = True):
        url = self.master + path
        headers = {"Accept": "application/json"}
        data = None
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self._token()}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            raise ApiError(_error_message(exc), exc.code) from None
        except urllib.error.URLError as exc:
            raise ApiError(f"Cannot reach the master at {self.master}: {exc.reason}. "
                           "Is it running (make up)? Set DC_MASTER for another address.") from None

    def _json(self, method: str, path: str, **kw) -> Any:
        with self._request(method, path, **kw) as resp:
            text = resp.read().decode("utf-8")
        return json.loads(text) if text else None

    def _token(self) -> str:
        if not self.token:
            # A master on this computer in development mode signs you in itself.
            try:
                self.token = self._json("POST", "/auth/local-token", auth=False, timeout=10)["token"]
            except ApiError as exc:
                if exc.status in (404, 405):
                    raise ApiError("This master needs a token: set DC_TOKEN (python generate_token.py "
                                   "--sub <you>) or pass --token") from None
                raise
        return self.token

    # -- API -----------------------------------------------------------------------

    def nodes(self) -> list[dict]:
        return self._json("GET", "/nodes")["items"]

    def runtimes(self) -> list[dict]:
        return self._json("GET", "/runtimes")["items"]

    def executions(self) -> list[dict]:
        return self._json("GET", "/executions")["items"]

    def execution(self, execution_id: str) -> dict:
        return self._json("GET", f"/executions/{_q(execution_id)}")

    def tasks(self, execution_id: str) -> dict:
        return self._json("GET", f"/executions/{_q(execution_id)}/tasks")

    def cancel(self, execution_id: str) -> dict:
        return self._json("POST", f"/executions/{_q(execution_id)}/cancel")

    def has_bundle(self, sha256: str) -> bool:
        try:
            self._json("GET", f"/bundles/{_q(sha256)}")
            return True
        except ApiError as exc:
            if exc.status == 404:
                return False
            raise

    def upload_bundle(self, archive: bytes) -> dict:
        return self._json("POST", "/bundles", raw=archive, content_type="application/gzip", timeout=1800)

    def create_execution(self, request: dict) -> dict:
        return self._json("POST", "/executions", body=request)

    def task_outputs(self, execution_id: str, task_id: str) -> bytes:
        with self._request("GET", f"/executions/{_q(execution_id)}/tasks/{_q(task_id)}/outputs",
                           timeout=1800) as resp:
            return resp.read()

    def set_env_cache(self, node_id: str, gb: float) -> dict:
        return self._json("PATCH", f"/nodes/{_q(node_id)}", body={"envCacheGb": gb})


def _q(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8") or "{}")
    except ValueError:
        body = {}
    message = body.get("error") or body.get("detail") or body.get("message")
    if exc.code == 401:
        return "The master did not accept the token (expired or for another master). Set a new DC_TOKEN."
    return str(message) if message else f"HTTP {exc.code} from the master"
