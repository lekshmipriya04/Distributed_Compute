"""ORM models for SSH-shared provider nodes and parallel task batches."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, String, Integer, Float, LargeBinary, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.common.database import Base


class SSHNode(Base):
    """A provider machine the master can borrow cores from over SSH.

    Credentials are stored encrypted (see crypto.py); the host key is pinned on
    first contact (TOFU) and verified on every subsequent connection.
    """

    __tablename__ = "ssh_nodes"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    # Connection
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    # managed_key (platform-generated key installed during onboarding) is the
    # only value new nodes get. password | private_key are legacy rows that the
    # "re-secure" flow converts to managed_key.
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False, default="managed_key")
    # Fernet ciphertext: the managed private key (OpenSSH PEM) for managed_key,
    # or the legacy password/private key for legacy rows.
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    host_key_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    host_key_b64: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Managed key metadata (public half is not secret).
    public_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Comment on the authorized_keys line, "<prefix>:<id>", used to remove it.
    key_comment: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    key_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Auto-detected hardware
    detected_cpu: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detected_memory_gb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    detected_gpu: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    os_info: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Allocation the operator chose to borrow (must be <= detected)
    allocated_cpu: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    allocated_memory_gb: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # GPU slice: devices 0..allocated_gpu_count-1, each capped at
    # allocated_gpu_memory_gb (per device, <= gpu_memory_gb). 0 = no GPU.
    allocated_gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    allocated_gpu_memory_gb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")

    # Cross-platform capabilities (Phase 2 discovery)
    os_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # linux|macos|windows
    gpu_vendor: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    gpu_backend: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    gpu_model: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gpu_memory_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # PyTorch GPU backend already on the worker ("torch-cuda" | "torch-mps"),
    # detected without installing anything. Only then is a GPU cap enforceable.
    gpu_runtime: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    python_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Languages/toolchains already present on the node, detected over SSH
    # (nothing is installed): JSON {runtime_id: version_string}. See runtimes.py.
    runtimes_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The SSH user's login-shell PATH, captured at detection time and used as
    # PATH inside the sandbox's clean environment (POSIX only).
    env_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    execution_backend: Mapped[str] = mapped_column(String(32), nullable=False, default="ssh")
    resource_enforcement: Mapped[str] = mapped_column(String(16), nullable=False, default="best_effort")
    capabilities_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Disk the owner lends for cached project environments in
    # ~/.cache/distributed-compute (0 = build each environment in the sandbox).
    env_cache_gb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")

    # Master-driven SSH health (no permanent agent)
    health_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    last_health_check: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="online")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @property
    def allocated_gpu_fraction(self) -> float:
        """Share of each allocated GPU's memory a job may use (0..1)."""
        from runtimes import gpu_fraction

        return gpu_fraction(self.allocated_gpu_count, self.allocated_gpu_memory_gb, self.gpu_memory_gb)


class PendingProvision(Base):
    """A key that was generated + installed on a node during onboarding but not
    yet registered as an :class:`SSHNode`. Expires after
    ``pending_provision_ttl_seconds``; an expired row's key line should be
    removed from the node by the recovery sweep when reachable.
    """

    __tablename__ = "pending_provisions"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    host_key_type: Mapped[str] = mapped_column(String(64), nullable=False)
    host_key_b64: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    key_comment: Mapped[str] = mapped_column(String(128), nullable=False)
    # installed | awaiting_manual_install
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="installed")
    # Detection result (NodeCapabilities as JSON) captured over the key login.
    capabilities_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ResourceLease(Base):
    """A bounded reservation of a node's borrowed cores/memory for one job.

    Node capacity is always derived as ``allocated − Σ(active lease allocations)``
    rather than a mutable counter, so it self-heals when a lease is released,
    expires, or is recovered after a crash.
    """

    __tablename__ = "resource_leases"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Free-form job identifier (batch id, training-run id, execution id, ...).
    job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ssh_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    cpu_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    memory_requested_gb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gpu_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cpu_allocated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    memory_allocated_gb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gpu_allocated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # pending | reserved | running | released | expired | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved", index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskBatch(Base):
    """A group of tasks the master fans out across borrowed nodes in parallel."""

    __tablename__ = "task_batches"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    # Runtime id from runtimes.py (python, bash, node, go, c, ..., custom).
    runtime: Mapped[str] = mapped_column(String(32), nullable=False, default="python")
    # Only for runtime == "custom": command template containing "{file}" and
    # the source file name written into the sandbox (e.g. "main.zig").
    custom_command: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    custom_filename: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    cpu_per_task: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    # safe_background | balanced | max_available (see resource_spec).
    execution_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="safe_background", server_default="safe_background"
    )
    # JSON list of node ids the user restricted the run to (empty = all online).
    node_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Project runs (runtime "project"): JSON of schemas.ProjectSpec.
    project_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # pending | running | cancelling | cancelled | completed |
    # completed_with_errors | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    tasks: Mapped[list["Task"]] = relationship(
        lambda: Task, back_populates="batch", cascade="all, delete-orphan", lazy="selectin"
    )


class Task(Base):
    """A single unit of work run once in its own remote sandbox.

    status: pending | running | completed | failed | cancelled
    """

    __tablename__ = "tasks"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_batches.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_data: Mapped[str] = mapped_column(Text, nullable=False, default="")
    node_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stdout: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stderr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Project runs: files brought back from the output folders (None = none
    # were collected; the archive is in the output store under the task id).
    output_files: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    batch: Mapped["TaskBatch"] = relationship(lambda: TaskBatch, back_populates="tasks")


class TrainingRun(Base):
    """A distributed ML training job run over borrowed cores (federated averaging).

    The dataset is split into shards and a partial model is trained on each
    (locally across cores, or remotely across SSH providers); the master
    averages the shard weights into one model, optionally over several rounds.
    JSON blobs hold the request/results so the schema stays model-agnostic.
    """

    __tablename__ = "training_runs"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dataset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model_type: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)  # single | multi
    engine: Mapped[str] = mapped_column(String(16), nullable=False, default="python")
    rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    hyperparams_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # pending | running | completed | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Results (populated as the run progresses)
    metrics_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    weights_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    history_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shards_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # What actually ran: engine, data loading, Ray parameter-server stats, Spark
    # model details (feature importances, grid search, labels).
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Source of a "custom" model (the user's model.py), run on the workers.
    model_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Zipped Spark PipelineModel for "spark:*" models (deferred: not loaded
    # unless the model is downloaded).
    artifact: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, deferred=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
