# Distributed Compute

[![CI](https://github.com/lekshmipriya04/Distributed_Compute/actions/workflows/ci.yml/badge.svg)](https://github.com/lekshmipriya04/Distributed_Compute/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Version 2.0.0](https://img.shields.io/badge/version-2.0.0-informational)
[![VS Code extension](https://img.shields.io/visual-studio-marketplace/v/aaron43210.distributed-ai-compute?label=VS%20Code%20extension)](https://marketplace.visualstudio.com/items?itemName=aaron43210.distributed-ai-compute)

**Run code and train models on the computers you already have, over plain
SSH.** Lend a few cores of a lab machine, an old laptop or a server, then run
any language on them in parallel, or train a model across all of them, from
VS Code or a web dashboard.

Workers need only `sshd` and `python3`. **Nothing is installed on them.** You
type a worker's password once; the platform installs a restricted key, forgets
the password, and removes the key when you remove the worker.

![VS Code running a file as 8 parallel tasks on two workers, with live output](distributed-ai-platform/distributed-compute-vscode/media/readme/13-live-output.png)

## What you can do

- **Add workers** with their SSH password, once. The platform shows the host
  key to confirm, detects CPU, memory, GPU and languages, and lets you choose
  how much of each machine to lend.
- **Run code in any language** your workers have (Python, Bash, Node.js, Go,
  Rust, Java, C/C++, Ruby, R, Julia, … or any command you give), as many
  parallel tasks, each in a throwaway sandbox. Watch every task's output live,
  cancel, retry only the failed tasks, and copy the output (VS Code can also
  save every task's output to files).
- **Train models** across your workers: built-in linear and logistic
  regression and a small CNN, **your own `model.py`** using any library the
  workers have, or, with the optional Spark + Ray engine, **any Spark MLlib
  model**. Download the trained model (ONNX, JSON or a Spark pipeline).
- **Share it** with a team: each person signs in with their own token and sees
  only their own workers, runs and data.

| | |
|---|---|
| ![Workers](distributed-ai-platform/frontend/docs/images/05-workers.png) | ![Training](distributed-ai-platform/frontend/docs/images/15-training-result.png) |
| The dashboard: workers with their slice, health and languages | A model trained across two workers |
| ![Results](distributed-ai-platform/frontend/docs/images/09-results.png) | ![Phone](distributed-ai-platform/frontend/docs/images/16-phone.png) |
| Each task's output, failures pulled out | On a phone |

## Quick start

You need [Docker](https://www.docker.com/products/docker-desktop/) (Docker
Desktop on Windows and macOS, Docker Engine or Desktop on Linux).

**From VS Code, with no terminal:** install the
[**Distributed AI Compute** extension](https://marketplace.visualstudio.com/items?itemName=aaron43210.distributed-ai-compute)
and click **Set Up Now** when it asks. It starts Docker if needed,
downloads the platform, starts it and connects. Then click **Add Compute
Node**.

**From a terminal:**

```bash
git clone https://github.com/lekshmipriya04/Distributed_Compute.git
cd Distributed_Compute/distributed-ai-platform
make up          # first start builds the images: several minutes
```

Open the dashboard at **http://localhost:3001**, go to **SSH Connection** and
add a worker. The [dashboard guide](distributed-ai-platform/frontend/README.md)
and the [extension guide](distributed-ai-platform/distributed-compute-vscode/README.md)
walk through every step with screenshots. Because the platform runs inside Docker:

| Worker | Address to enter |
|---|---|
| This computer | `host.docker.internal` (turn on SSH / Remote Login first) |
| Another machine on your network | its IP address, e.g. `192.168.1.20` |
| Two test workers, to try it out | run `make up-dev`, then `ssh-provider`, port `2222`, user `provider`, password `providerpass` |

Use a dedicated, non-admin account on each worker: tasks run with its
permissions.

`make down` stops everything and keeps your data; `make reset` deletes it.
`make backup` copies the database and datasets to `backups/<date>/`.

**Upgrading from 1.x?** Run `make up`, then `make migrate-datasets` to move
datasets out of HDFS. See [Upgrade from 1.x](distributed-ai-platform/docs/DEPLOYMENT.md#6-upgrade-from-1x-and-rollback).

## One computer, or a team

By default the platform is for **one person on one computer**: every port
listens on `127.0.0.1`, and you are the local user with no sign-in.

For **several people**, run it on one machine in production mode with HTTPS in
front, and give each person a token (`python generate_token.py --sub <name>`).
They sign in from the dashboard's account button or VS Code's **Configure
Authentication**. Each user sees only their own workers, runs, datasets and
models. Step-by-step: [docs/DEPLOYMENT.md](distributed-ai-platform/docs/DEPLOYMENT.md).

## Supported platforms

| | Supported | Tested |
|---|---|---|
| Platform (Docker) | Linux, macOS, Windows | macOS; CI runs it on Linux |
| VS Code extension | VS Code 1.90+ on Linux, macOS, Windows | real VS Code on macOS; CI runs Linux, macOS, Windows |
| Web dashboard | Chrome, Edge, Firefox, Safari; Android and iOS browsers | Chromium, Firefox, WebKit, Android and iPhone emulation (Playwright) |
| Workers | Linux and macOS with `sshd` + `python3`; Windows with OpenSSH | Linux; Windows workers not yet verified |

## How it works

```
VS Code extension ─► extension-gateway :8090 ─┐
Web dashboard :3001 ─► api-gateway :8000 ─────┼─► ssh-executor (the master) ──SSH──► your workers
                                              └─► storage-service ─► datasets volume
```

![Platform architecture](distributed-ai-platform/docs/architecture-overview.svg)

- **Onboarding.** The master shows the worker's host-key fingerprint before
  any credential is sent. Your password is used once to install a
  `restrict ssh-ed25519 … dp-platform:<id>` key, then dropped. If a worker
  refuses password logins, you get one command to run there instead.
- **Tasks.** Each task runs in its own temporary directory on the worker, with
  a clean environment, its own process group, `nice`, core pinning (Linux) and
  a timeout. Afterwards the processes are killed and the directory deleted:
  on success, failure, timeout or cancel.
- **Resources.** Cores and memory are reserved per worker so jobs never
  overcommit it. A GPU slice is enforced for Python tasks where PyTorch is
  already installed.
- **Training.** The master splits the dataset into shards sized by each
  worker's speed, sends each worker its shard and a small script (or your
  `model.py`), and averages the results over several rounds (federated
  averaging).
- **Datasets.** Files live in the Docker volume `datasets`; names, owners and
  sizes in Postgres. Uploads and downloads are streamed, so file size does not
  affect memory. Installs that already run HDFS can keep using it with
  `make up-hdfs` (adds 4 containers and about 1.4 GB of memory).
- **Spark + Ray (optional).** `make enable-spark-ray`, or **Enable Spark + Ray
  Engine** in VS Code, adds about 1 GB to the master (never to workers): every
  Spark MLlib model with grid search, Parquet data, and Ray for the parameter
  server and parallel local shards. Existing clusters can be used via
  `SPARK_MASTER_URL` and `RAY_ADDRESS`.

**Step-by-step guides with screenshots:**
[VS Code extension](distributed-ai-platform/distributed-compute-vscode/README.md) ·
[web dashboard](distributed-ai-platform/frontend/README.md)

More: [architecture](distributed-ai-platform/docs/architecture.md) ·
[ports and protocols](distributed-ai-platform/docs/connections.md) ·
[API](distributed-ai-platform/docs/API_CONTRACT.md) ·
[project overview](distributed-ai-platform/project.md)

## Development and tests

```bash
cd distributed-ai-platform
make init && pytest tests -q && make lint            # Python services
(cd services/extension-gateway && go test ./...)     # Go gateway
(cd frontend && npm ci && npm test && npm run build) # dashboard
(cd distributed-compute-vscode && npm ci && npm test && npm run test:vscode)
```

End-to-end tests run against the real stack (`make up-dev`) as three users on
three ports:

```bash
(cd frontend && npm run e2e:serve && npm run test:e2e)  # Chromium, Firefox, WebKit, Android, iOS
(cd distributed-compute-vscode && npm run test:e2e)     # needs gateways on 8091/8092, see the CI e2e job
```

For dashboard development with hot reload: `cd frontend && npm run dev`
(http://localhost:3000).

## Limits

- The sandbox isolates **files and processes, not the kernel**. Tasks can read
  the SSH user's files and use the network; that is why workers need a
  dedicated, non-admin account. A task that deliberately escapes its process
  group can outlive the job.
- Memory slices are a scheduling figure, not a kernel limit; cores are not
  pinned on macOS. The GPU cap is best effort and needs PyTorch on the worker.
- End-to-end tests use Docker test workers. **Physical remote machines and
  Windows workers have not been verified yet.**
- `ssh-executor` runs code you send on your workers: treat it as a privileged
  service. Put HTTPS in front of it when anyone else can reach it.
- Database changes are additive only; `make migrate-datasets` is the only
  data migration so far.

## Contributing

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Please read
the [Code of Conduct](CODE_OF_CONDUCT.md), and report security issues
privately as described in [SECURITY.md](SECURITY.md).

## Repository layout

```text
distributed-ai-platform/
├── docker-compose.yml, Makefile, generate_token.py
├── config/                      .env.example (settings and secrets)
├── frontend/                    web dashboard (React, Vite, nginx) + Playwright tests
├── distributed-compute-vscode/  VS Code extension
├── services/                    api-gateway, storage-service, ssh-executor (Python), extension-gateway (Go)
├── shared/                      shared Python library
├── infrastructure/docker/       Postgres init, Hadoop config (optional HDFS)
├── docs/                        architecture, API, deployment, images
└── tests/                       Python unit tests, load test
```

## Authors and license

Made by AARON R, M R LEKSHMI PRIYA and ALEENA VJ. Released under the
[MIT License](LICENSE).
