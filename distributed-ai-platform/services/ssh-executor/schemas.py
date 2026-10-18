"""Pydantic request/response schemas for the ssh-executor API.

This module is the API contract shared by the backend, the React frontend
(``frontend/src/api/ssh.ts``) and ``docs/API_CONTRACT.md``. Keep all three in
sync.

Secrets (the one-time onboarding password) only ever appear on *inbound*
request models; no response model exposes a password, a private key or
``secret_encrypted``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


class _CapabilityFields(BaseModel):
    """Node capabilities shared across provision + node responses."""

    os_type: Optional[str] = None  # linux | macos | windows
    os_info: Optional[str] = None
    detected_cpu: int = 0
    detected_memory_gb: float = 0.0
    detected_gpu: int = 0
    gpu_vendor: Optional[str] = None
    gpu_backend: Optional[str] = None
    gpu_model: Optional[str] = None
    gpu_count: int = 0
    gpu_memory_gb: Optional[float] = None
    # PyTorch GPU backend already on the node ("torch-cuda" | "torch-mps"), or
    # None. A GPU memory cap is only enforceable when this is set.
    gpu_runtime: Optional[str] = None
    python_version: Optional[str] = None
    # Languages already present on the node: {runtime_id: version}. Nothing is
    # installed by the platform; see runtimes.py for the registry.
    runtimes: dict[str, str] = Field(default_factory=dict)
    execution_backend: str = "ssh"
    resource_enforcement: str = "best_effort"


# --- onboarding (password -> managed key) ---------------------------------
class NodeConnectRequest(BaseModel):
    """Step 1: fetch the node's host key. No credential is sent."""

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)


class NodeConnectResponse(BaseModel):
    host: str
    port: int
    username: str
    host_key_type: str
    fingerprint: str  # "SHA256:..."
    # Signed, short-lived token binding host+port+username+host key+owner.
    onboarding_token: str
    # True if one of the caller's nodes already pins this exact host key.
    already_registered: bool = False


class NodeProvisionRequest(BaseModel):
    """Step 2: one-time password login that installs the managed key.

    The password is used for this request only and is never stored or logged.
    """

    onboarding_token: str
    password: str = Field(min_length=1, max_length=4096)


class NodeManualProvisionRequest(BaseModel):
    """Fallback when the node's sshd refuses password logins: generate the key
    and return the public half + a one-line install command for the user."""

    onboarding_token: str


class NodeManualProvisionResponse(BaseModel):
    provision_id: UUID
    public_key: str
    # Copy-paste command for the worker's terminal (POSIX shell or PowerShell,
    # chosen from os_hint when known, else both are returned).
    install_command_posix: str
    install_command_windows: str
    expires_at: datetime


class NodeProvisionResponse(_CapabilityFields):
    """Result of a successful key install + key-only verification + detection."""

    provision_id: UUID
    host: str
    port: int
    username: str
    fingerprint: str
    expires_at: datetime


class NodeRegisterRequest(BaseModel):
    """Step 3: persist the provisioned node with the chosen allocation.

    Detected values are NOT accepted from the client; the server uses the ones
    it captured during provisioning.
    """

    provision_id: UUID
    name: str = Field(default="", max_length=256)  # empty -> host
    allocated_cpu: int = Field(default=1, ge=1)
    allocated_memory_gb: float = Field(default=1.0, ge=0.0)
    # GPU slice (clamped to the detected GPU; per-device memory cap).
    allocated_gpu_count: int = Field(default=0, ge=0)
    allocated_gpu_memory_gb: float = Field(default=0.0, ge=0.0)


class NodeResecureRequest(BaseModel):
    """Convert a legacy password/private_key node to a managed key."""

    password: Optional[str] = Field(default=None, max_length=4096)


class NodeUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=256)
    # "draining" keeps running work but gives the node no new tasks or
    # training shards; "online" takes new work again.
    status: Optional[str] = Field(default=None, pattern="^(online|draining)$")
    allocated_cpu: Optional[int] = Field(default=None, ge=1)
    allocated_memory_gb: Optional[float] = Field(default=None, ge=0.0)
    allocated_gpu_count: Optional[int] = Field(default=None, ge=0)
    allocated_gpu_memory_gb: Optional[float] = Field(default=None, ge=0.0)
    # Disk lent for cached project environments (0 turns the cache off).
    env_cache_gb: Optional[float] = Field(default=None, ge=0.0, le=10_000)


class NodeResponse(_CapabilityFields):
    id: UUID
    owner_id: str
    name: str
    host: str
    port: int
    username: str
    # managed_key | password (legacy) | private_key (legacy)
    auth_type: str
    fingerprint: Optional[str] = None  # host key fingerprint
    key_created_at: Optional[datetime] = None
    allocated_cpu: int
    allocated_memory_gb: float
    allocated_gpu_count: int = 0
    allocated_gpu_memory_gb: float = 0.0
    # allocated_gpu_memory_gb / gpu_memory_gb, clamped 0..1 (0 = no GPU).
    allocated_gpu_fraction: float = 0.0
    env_cache_gb: float = 0.0
    status: str
    health_status: str = "unknown"
    last_health_check: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class NodeListResponse(BaseModel):
    items: list[NodeResponse]
    total: int


class NodeDeleteResponse(BaseModel):
    deleted: bool
    # False when the node was unreachable and the authorized_keys line remains.
    key_removed: bool
    message: str


# --- languages / runtimes ---------------------------------------------------
class RuntimeInfo(BaseModel):
    id: str  # e.g. "python", "node", "go", "c", "custom"
    name: str  # display name
    filename: str  # source file written into the sandbox, e.g. "main.go"
    kind: str  # interpreted | compiled | custom
    example_code: str
    windows_supported: bool
    # Online nodes of the caller that have this runtime available right now.
    available_node_ids: list[UUID] = Field(default_factory=list)


class RuntimeListResponse(BaseModel):
    items: list[RuntimeInfo]


# --- task batches -----------------------------------------------------------
class ProjectDataset(BaseModel):
    """A dataset copied into the project folder before the command runs."""

    dataset_id: UUID
    # Where it goes, relative to the project; default data/<name>.<format>.
    path: Optional[str] = Field(default=None, max_length=512)


class ProjectSpec(BaseModel):
    """Run a folder of code: the bundle from ``POST /bundles`` and a command.

    ``requirements`` is a pip requirements file inside the project; the worker
    builds (or reuses) a virtual environment from it. Files written to the
    ``outputs`` folders are brought back and can be downloaded per task.
    """

    bundle_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    command: str = Field(min_length=1, max_length=4096)
    requirements: Optional[str] = Field(default=None, max_length=512)
    outputs: list[str] = Field(default_factory=lambda: ["outputs"], max_length=20)
    datasets: list[ProjectDataset] = Field(default_factory=list, max_length=20)

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty")
        return value

    @field_validator("requirements")
    @classmethod
    def _requirements_path(cls, value: Optional[str]) -> Optional[str]:
        from projects import check_relative_path

        return check_relative_path(value, "requirements") if value else None

    @field_validator("outputs")
    @classmethod
    def _output_paths(cls, value: list[str]) -> list[str]:
        from projects import check_relative_path

        return list(dict.fromkeys(check_relative_path(v, "outputs") for v in value))

    @field_validator("datasets")
    @classmethod
    def _dataset_paths(cls, value: list[ProjectDataset]) -> list[ProjectDataset]:
        from projects import check_relative_path

        for item in value:
            if item.path:
                item.path = check_relative_path(item.path, "datasets[].path")
        return value


class BatchCreateRequest(BaseModel):
    """Code plus one input per task, or a project plus one input per task.

    ``runtime`` is a runtime id from ``GET /runtimes``. ``custom`` runs
    ``custom_command`` (must contain ``{file}``) after writing ``code`` to
    ``custom_filename`` inside the sandbox, so any language whose toolchain is
    already on the worker can be used. With ``project`` set, ``code`` is not
    used and the runtime is ``project``.
    """

    code: str = ""
    inputs: list[str] = Field(default_factory=list)
    node_ids: list[UUID] = Field(default_factory=list)
    runtime: str = Field(default="python", min_length=1, max_length=32)
    custom_command: Optional[str] = Field(default=None, max_length=2048)
    custom_filename: Optional[str] = Field(default=None, max_length=128)
    cpu_per_task: int = Field(default=1, ge=1)
    timeout_seconds: Optional[int] = Field(default=None, ge=1)
    execution_policy: str = Field(
        default="safe_background", pattern="^(safe_background|balanced|max_available)$"
    )
    project: Optional[ProjectSpec] = None

    @model_validator(mode="after")
    def _project_runtime(self) -> "BatchCreateRequest":
        if self.project is not None:
            self.runtime = "project"
        elif self.runtime == "project":
            raise ValueError("runtime 'project' needs a project (bundle_id and command)")
        return self


class TaskResponse(BaseModel):
    id: UUID
    batch_id: UUID
    seq: int
    node_id: Optional[UUID]
    status: str
    exit_code: Optional[int]
    stdout: Optional[str]
    stderr: Optional[str]
    attempts: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    # Project runs: files collected from the output folders (download them
    # from GET /batches/{id}/tasks/{task_id}/outputs when > 0).
    output_files: Optional[int] = None
    output_bytes: Optional[int] = None

    model_config = {"from_attributes": True}


class BatchResponse(BaseModel):
    id: UUID
    owner_id: str
    runtime: str
    status: str
    message: Optional[str] = None
    total: int
    completed: int
    failed: int
    cancelled: int = 0
    cpu_per_task: int = 1
    timeout_seconds: int = 120
    execution_policy: str = "safe_background"
    created_at: datetime
    finished_at: Optional[datetime] = None
    project_json: Optional[str] = Field(default=None, exclude=True)

    model_config = {"from_attributes": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def project(self) -> Optional[ProjectSpec]:
        if not self.project_json:
            return None
        try:
            return ProjectSpec.model_validate_json(self.project_json)
        except ValueError:
            return None


class BundleResponse(BaseModel):
    id: str  # sha256 of the archive
    size_bytes: int
    files: Optional[int] = None
    unpacked_bytes: Optional[int] = None


class BatchListResponse(BaseModel):
    items: list[BatchResponse]


class BatchTasksResponse(BaseModel):
    batch: BatchResponse
    tasks: list[TaskResponse]


# --- distributed ML training ------------------------------------------------
TRAIN_MODEL_PATTERN = (
    r"^(linear_regression|logistic_regression|cnn|custom|spark:[A-Za-z][A-Za-z0-9]{1,63})$"
)


class TrainRequest(BaseModel):
    """Train a model on the platform.

    model_type:
      * ``linear_regression`` | ``logistic_regression`` | ``cnn``: built-in
        stdlib models, federated over the workers (or local cores).
      * ``custom``: the user's ``model_code`` (a model.py with ``train()``),
        federated over the workers. Needs ``mode="multi"``.
      * ``spark:<Estimator>``: any Spark MLlib estimator from
        ``GET /engines/spark_ray/models``, trained by Spark on the master.
        Needs ``engine="spark_ray"``.

    engine:
      * ``python``: pure Python on the master (default, always available).
      * ``spark_ray``: Spark loads the data (CSV/JSON/Parquet) and runs MLlib
        models; Ray holds the global model and trains local shards. Master
        only. Worker nodes never need anything installed.
    """

    model_config = {"protected_namespaces": ()}

    dataset_id: UUID
    model_type: str = Field(pattern=TRAIN_MODEL_PATTERN)
    engine: str = Field(default="python", pattern="^(python|spark_ray)$")
    # Optional only for clustering models, which have no target.
    target_column: str = Field(default="", max_length=256)
    feature_columns: list[str] = Field(default_factory=list)
    mode: str = Field(default="multi", pattern="^(single|multi)$")
    local_cores: int = Field(default=2, ge=1)
    learning_rate: float = Field(default=0.01, gt=0)
    epochs: int = Field(default=10, ge=1)
    rounds: int = Field(default=3, ge=1)
    num_classes: int = Field(default=0, ge=0)
    node_ids: list[UUID] = Field(default_factory=list)
    execution_policy: str = Field(
        default="safe_background", pattern="^(safe_background|balanced|max_available)$"
    )
    # custom: the model.py source. Size is capped by MAX_CODE_BYTES.
    model_code: Optional[str] = None
    # custom: merged into the config passed to init/train/evaluate.
    # spark:*: estimator parameters, e.g. {"numTrees": 50}.
    model_params: dict = Field(default_factory=dict)
    # spark:*: values to try, e.g. {"maxDepth": [3, 5, 8]}; the best on a
    # validation split (Spark TrainValidationSplit) is kept.
    param_grid: dict[str, list] = Field(default_factory=dict)
    # spark:*: share of rows held out to report test metrics.
    test_fraction: float = Field(default=0.2, ge=0.05, le=0.5)
    # custom: the model uses the GPU, so GPU nodes get bigger shards.
    uses_gpu: bool = False
    seed: int = Field(default=42, ge=0)


class TrainRunResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: UUID
    owner_id: str
    dataset_id: UUID
    model_type: str
    model_family: Optional[str] = None  # builtin | custom | spark
    engine: Optional[str] = None
    mode: str
    rounds: int
    status: str
    message: Optional[str] = None
    metrics: Optional[dict] = None
    history: Optional[list] = None
    shards: Optional[list] = None
    # What actually ran: engine, data loader, Ray and Spark details.
    details: Optional[dict] = None
    # ONNX (built-in), params.json (custom) or a Spark model zip (spark:*).
    model_format: Optional[str] = None
    created_at: datetime


class EngineInfo(BaseModel):
    id: str
    name: str
    available: bool
    missing: list[str] = Field(default_factory=list)
    versions: dict[str, Optional[str]] = Field(default_factory=dict)
    spark_master: Optional[str] = None
    ray_address: Optional[str] = None
    hint: Optional[str] = None


class EngineListResponse(BaseModel):
    items: list[EngineInfo]


class SparkModelParam(BaseModel):
    name: str
    type: str
    default: Optional[object] = None
    has_default: bool = True
    required: bool = False
    doc: str = ""


class SparkModelInfo(BaseModel):
    id: str
    name: str
    family: str
    needs_target: bool
    params: list[SparkModelParam]


class SparkModelListResponse(BaseModel):
    items: list[SparkModelInfo]
