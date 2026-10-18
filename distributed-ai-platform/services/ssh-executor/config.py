from functools import lru_cache
from typing import Optional

from pydantic import Field

from shared.common.config import BaseServiceSettings

# Known placeholder values that must never be used outside development.
INSECURE_DEFAULT_SECRETS = frozenset({"change_me_ssh_cred_key", "change_me_in_production"})


class SSHExecutorSettings(BaseServiceSettings):
    service_name: str = "ssh-executor"
    port: int = Field(default=8011)

    # Symmetric key material used to encrypt stored SSH private keys at rest.
    # Any string works — it is hashed to a 32-byte Fernet key (see crypto.py).
    # The service refuses to start outside development while this is a default
    # (see security.py).
    ssh_cred_key: str = Field(default="change_me_ssh_cred_key")

    # --- onboarding (password -> managed key) ----------------------------
    # Lifetime of the signed token returned by /nodes/connect that binds
    # host+port+username+host key between the connect and provision steps.
    onboarding_token_ttl_seconds: int = Field(default=600, ge=30)
    # How long a provisioned-but-not-yet-registered key is kept (pending row).
    pending_provision_ttl_seconds: int = Field(default=900, ge=60)
    # Comment prefix written on every authorized_keys line we install, so the
    # line can be found and removed again. Final comment: "<prefix>:<id>".
    managed_key_comment_prefix: str = Field(default="dp-platform")
    # Options prepended to the installed public key. "restrict" disables
    # forwarding/pty/agent while still allowing exec + sftp.
    managed_key_options: str = Field(default="restrict")

    # --- network guardrails ----------------------------------------------
    # Optional allowlist of hostnames/IPs that may be registered as providers.
    # Empty = allow any (subject to the CIDR rules below).
    allowed_hosts: list[str] = Field(default_factory=list)
    # Optional allowlist of CIDRs. Empty = allow any non-blocked address.
    allowed_cidrs: list[str] = Field(default_factory=list)
    # Block loopback / link-local / unspecified targets (SSRF guard). The dev
    # compose providers are reached by docker DNS names on a private bridge,
    # which are not loopback, so this stays on by default.
    block_loopback_and_link_local: bool = Field(default=True)

    # --- per-task guardrails ---------------------------------------------
    connect_timeout: int = Field(default=10, ge=1)
    task_timeout_seconds: int = Field(default=120, ge=1)
    max_task_timeout_seconds: int = Field(default=3600, ge=1)
    max_output_bytes: int = Field(default=1_000_000, ge=1024)
    # Priority (nice) applied to remote tasks so the provider is not disturbed.
    task_nice: int = Field(default=19, ge=0, le=19)

    # --- generic user-code execution -------------------------------------
    max_code_bytes: int = Field(default=1_000_000, ge=1024)
    max_inputs_per_batch: int = Field(default=10_000, ge=1)
    # Runtime ids (see runtimes.py) users may select. ["*"] = every registered
    # runtime, including "custom".
    allowed_runtimes: list[str] = Field(default_factory=lambda: ["*"])

    # --- projects (a folder + command, see projects.py) --------------------
    # Bundles, dataset copies and task outputs are kept here (a Docker volume).
    project_data_dir: str = Field(default="/var/lib/distributed-compute")
    max_bundle_bytes: int = Field(default=200 * 1024 * 1024, ge=1024)
    max_bundle_unpacked_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1024)
    max_bundle_files: int = Field(default=50_000, ge=1)
    # Per task: files from the output folders beyond this are left out.
    max_project_output_bytes: int = Field(default=500 * 1024 * 1024, ge=1024)
    env_build_timeout_seconds: int = Field(default=1800, ge=10)
    lease_ttl_seconds: int = Field(default=3600, ge=1)
    # How often the lease/stale-job recovery loop runs.
    recovery_interval_seconds: int = Field(default=60, ge=5)
    # Remote sandboxes older than this are swept on connect (crash leftovers).
    stale_sandbox_minutes: int = Field(default=180, ge=1)

    # --- distributed ML training -----------------------------------------
    storage_service_url: str = Field(default="http://storage-service:8001")
    training_timeout_seconds: int = Field(default=600, ge=1)
    max_train_rows: int = Field(default=200_000, ge=1)
    # Compute-weighted sharding: a node's score is
    #   gpu_memory_gb * shard_weight_gpu + cores * shard_weight_cpu
    # and shard sizes are proportional to it. The GPU term only counts when the
    # job can really use the GPU (see training_orchestrator.plan_shard_sizes).
    shard_weight_gpu: float = Field(default=5.0, ge=0.0)
    shard_weight_cpu: float = Field(default=1.0, ge=0.0)
    # Model parameters come back in task output, so training tasks get a larger
    # output cap than ordinary batches.
    max_train_output_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    # A trained Spark model is stored in the database as a zip.
    max_model_artifact_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)

    # --- optional Spark + Ray engine (master only) -------------------------
    # Installed only when the image is built with WITH_SPARK_RAY=true, or when
    # the master's own Python already has pyspark, ray and a Java runtime.
    # "local[*]" uses the master's cores; set a spark:// URL to use a cluster.
    spark_master_url: str = Field(default="local[*]")
    spark_driver_memory: str = Field(default="2g")
    # Rows a Spark MLlib model may train on (it trains on the master, so it is
    # not limited by max_train_rows, which bounds what is sent to workers).
    max_spark_rows: int = Field(default=5_000_000, ge=1)
    # Empty = start Ray on the master. Set to an existing cluster ("ray://...")
    # to use it instead.
    ray_address: str = Field(default="")
    ray_num_cpus: Optional[int] = Field(default=None, ge=1)

    model_config = {"frozen": True}


@lru_cache(maxsize=1)
def get_settings() -> SSHExecutorSettings:
    return SSHExecutorSettings()
