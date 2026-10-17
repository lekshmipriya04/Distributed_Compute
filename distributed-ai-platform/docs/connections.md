# Connections

Every network edge in the platform: who talks to whom, on which port, with
which protocol and which credentials. For the architecture, see
[architecture.md](architecture.md). For request shapes, see
[API_CONTRACT.md](API_CONTRACT.md).

## 1. Ports

| Component | Container | Host port | Protocol | Notes |
|---|---|---|---|---|
| Frontend (Vite dev server) | not in Compose | 3000 | HTTP | Proxies `/api` and `/health` to `localhost:8000` |
| api-gateway | `api-gateway` | 8000 | HTTP | Only ingress for the browser |
| storage-service | `storage-service` | 8001 | HTTP | Dataset files in the `datasets` volume at `/data` |
| ssh-executor | `ssh-executor` | 8011 | HTTP | Outbound SSH to workers |
| extension-gateway (Go) | `extension-gateway` | 8090 | HTTP | For the VS Code extension |
| PostgreSQL 15 | `postgres` | 5432 | PostgreSQL | |
| HDFS NameNode (optional) | `namenode` | 9870 (WebHDFS/UI), 9000 (RPC) | HTTP / Hadoop RPC | `--profile hdfs` only; 3 DataNodes on the internal network |
| SSH dev providers | `ssh-provider`, `ssh-provider-2` | not published | SSH :2222 | `--profile dev` only; login `provider/providerpass` |
| Workers | your machines | 22 (or as registered) | SSH | `sshd` + `python3`, nothing else |

Every container is on the `platform-net` bridge (172.20.0.0/16) and
addresses the others by container name.

## 2. Edges

### 2.1 Clients
| From | To | Protocol | Auth |
|---|---|---|---|
| Browser | Vite :3000 → api-gateway :8000 `/api/v1/*` | HTTP/JSON (multipart for uploads) | `Authorization: Bearer <JWT>` (anonymous `dev-user` outside production) |
| Scripts / REST clients | api-gateway :8000 | HTTP/JSON | Bearer JWT |
| VS Code extension | extension-gateway :8090 | HTTP/JSON, `X-Request-Id` on submits | Bearer JWT from VS Code SecretStorage |

### 2.2 Gateways → services
| From | To | Path mapping | Auth |
|---|---|---|---|
| api-gateway | storage-service :8001 | `/api/v1/storage/{p}` → same path | Caller's `Authorization` forwarded |
| api-gateway | ssh-executor :8011 | `/api/v1/ssh/{p}` → same path | Caller's `Authorization` forwarded |
| extension-gateway | ssh-executor :8011 (`UPSTREAM_URL`) | `/nodes` → `/api/v1/ssh/nodes` (+ `/nodes/{id}/capacity`), `/runtimes` → `/runtimes`, `/executions` → `/batches`; `/cluster/status` is computed from the node list | Caller's JWT forwarded; the gateway has no identity of its own |

### 2.3 Services → infrastructure and each other
| From | To | Protocol | Purpose |
|---|---|---|---|
| storage-service | Postgres :5432 | asyncpg | `datasets` metadata |
| storage-service | `/data` (volume `datasets`) | local files | Dataset bytes (default) |
| storage-service | NameNode :9870 → DataNodes | WebHDFS (HTTP, redirect to a DataNode) | Dataset bytes with `STORAGE_BACKEND=hdfs`, and older datasets until migrated |
| ssh-executor | Postgres :5432 | asyncpg | Nodes, pending provisions, leases, batches, tasks, training runs |
| ssh-executor | storage-service :8001 | HTTP | Training reads dataset metadata and bytes with an internal JWT (`internal: true`) plus `X-On-Behalf-Of: <run owner>` |

### 2.4 Master → workers (ssh-executor only)
| Step | Auth | What crosses the wire |
|---|---|---|
| Connect (onboarding step 1) | none (host key read only) | Host key → SHA256 fingerprint shown to the user |
| Provision (step 2) | Password, once, over the pinned host key | Appends `restrict <ed25519 pubkey> dp-platform:<id>` to authorized keys; capability and runtime detection |
| Everything after | Managed Ed25519 key, pinned host key | Sandbox create, SFTP upload of code, input and runner, the launch command with stdin, stdout/stderr, cancel, cleanup, stale sweep, health check, refresh, key rotation and removal |

Data in transit to a worker: the task code, the task input (stdin and
`input.txt`), and for training a stdlib script plus one data shard. Nothing
persists on the worker except the authorized-keys line.

## 3. Authentication summary

- **User JWT (HS256, `JWT_SECRET`)**: checked by storage-service and
  ssh-executor. The gateways only forward it. `ENVIRONMENT=production` makes it
  mandatory.
- **Internal JWT**: minted by ssh-executor (`create_internal_token`, 5 min TTL).
  storage-service honours `X-On-Behalf-Of` only with this token.
- **SSH**: TOFU at onboarding, then pinned. A changed host key is rejected
  (`host_key_mismatch`). Private keys are encrypted at rest with `SSH_CRED_KEY`.
- **Outside development**, services refuse placeholder or short secrets.

## 4. Sequences

### 4.1 Add a worker
```
UI ─POST /nodes/connect {host,port,user}──▶ ssh-executor ─(read host key)─▶ worker
UI ◀─ fingerprint + onboarding_token ──────
UI ─POST /nodes/provision {token,password}─▶ ssh-executor ─password login─▶ worker
                                             install restricted key, re-login with key,
                                             detect OS/CPU/RAM/GPU/torch/runtimes
UI ◀─ detected capabilities + provision_id ─
UI ─POST /nodes {provision_id, slice}──────▶ ssh-executor (row created; password gone)
```

### 4.2 Run a batch
```
UI ─POST /batches {runtime, code, inputs, nodes, cpu_per_task, policy}─▶ ssh-executor
     400 if no selected online node has the runtime; else 201 and background dispatch
ssh-executor: lease each node (SELECT … FOR UPDATE) → slots per node
  per task:  mktemp -d dp-exec.<exec_id>.XXXXXX → upload → runner (nice/taskset/timeout)
             → stdout/stderr/exit → rm -rf sandbox
UI ─GET /batches/{id}/tasks (poll)─▶ per-task output
UI ─POST /batches/{id}/cancel─▶ status cancelling → kill process group by exec id → cancelled
```

### 4.3 Train
```
UI ─POST /storage/datasets (or /datasets/images)─▶ storage-service → /data + Postgres
UI ─POST /ssh/train {dataset_id, model_type, mode,...}─▶ ssh-executor
ssh-executor ─GET dataset (internal JWT + X-On-Behalf-Of)─▶ storage-service
multi:  lease nodes → compute-weighted shards → stdlib script + shard per worker sandbox
single: ProcessPoolExecutor on the master
master: FedAvg per round → ONNX (GET /train/{id}/model)
```

### 4.4 VS Code
```
extension ─GET /nodes, /runtimes, /executions─▶ extension-gateway ─▶ ssh-executor
extension ─POST /executions {runtime, code, executionPolicy}─▶ gateway ─POST /batches─▶ ssh-executor
```

## 5. Removed edges

The following do not exist any more, and nothing should call them: Spark
master and workers, Livy, Ray, MLflow, Kafka/Zookeeper, Prometheus/Grafana,
preprocessing, training, evaluation, registry and serving services, the
worker registry, the resource manager, the scheduler, the `worker/` agent and
its gateway WebSocket. Services still expose `/metrics`, but no Prometheus is
deployed to scrape them.
