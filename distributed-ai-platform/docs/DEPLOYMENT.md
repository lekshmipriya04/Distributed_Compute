# Deployment plan: Distributed Compute

Release runbook for **2.0.0**: the platform (Python services, Go gateway,
Docker setup), the web dashboard and the VS Code extension
(`aaron43210.distributed-ai-compute`). Steps are in order; each has a check
that says it worked.

**Versions.** From 2.0.0 everything shares one version number. The extension
downloads the platform release with its own version (`PLATFORM_RELEASE` in
`distributed-compute-vscode/src/models/platform.ts`, tag `v2.0.0`); a unit
test fails if the two ever differ.

**What 2.0.0 changes.** Datasets are stored as plain files in the Docker
volume `datasets` instead of HDFS. A fresh start runs 6 containers instead of
10, reaches the first upload in about 23 s instead of 33 s, and uses about
0.5 GB of memory instead of 1.9 GB (images 2.1 GB instead of 5.9 GB; measured
with `scripts/measure-startup.sh`). HDFS is still available with
`make up-hdfs`. Existing installs move their datasets with
`make migrate-datasets` (section 6).

## 1. Where things stand

| Area | Verified on | How |
|---|---|---|
| Extension unit tests (84) | macOS | `npm test` |
| Extension in a real VS Code 1.142 | macOS | `npm run test:vscode`, offline and against gateways on ports 8091 and 8092 |
| Every step of the extension README, driven in a real VS Code | macOS + Docker | `npm run docs:screenshots` (also re-creates the 20 README images) |
| Every step of the dashboard README, in the production image | macOS + Docker | `npm run docs:screenshots` in `frontend/` (16 images) |
| Multi-user end to end (19 tests) | macOS + Docker | `npm run test:e2e`: 3 users on ports 8090/8091/8092, 2 SSH test workers |
| `.vsix` packages as `aaron43210.distributed-ai-compute`, README image links point into the repository | macOS | `npm run package` |
| Gateway builds for linux, darwin, windows (amd64, arm64) | macOS cross-compile | `go build` with `GOOS`/`GOARCH` |
| Python services (133 tests), ruff, mypy | macOS | `pytest`, `make lint` |
| Dashboard unit tests (29), ESLint, build | macOS | `npm test`, `npm run lint`, `npm run build` |
| Dashboard in Chromium, Firefox, WebKit, Android (Pixel 7) and iPhone 14 emulation (53 tests) | macOS + Docker | `npm run test:e2e`: the nginx image on ports 4101/4102/4103 as three users, 2 SSH test workers |
| Extension and dashboard build on **Windows and Linux** | **not yet**: first CI run | `extension` and `frontend` jobs, OS matrix |
| Full stack in CI | **not yet**: first CI run | `e2e` job |
| **Windows worker machines** | **never** | implemented, not tested on real Windows hosts |
| Physical remote workers | **never** | only Docker test workers so far |

The end-to-end suite covers: one-click and JWT sign-in; missing, forged and
expired tokens; identity following the token, not the port; users unable to see
or change each other's nodes, runs, datasets and models; wrong SSH password and
unreachable host with readable reasons; adding nodes; health check,
re-detection, re-slicing, key rotation, drain and resume; Python and Bash runs;
four tasks spread over two workers in parallel (5.4 s instead of 16 s);
partial failure and retry of failed tasks only; cancel; timeout; dataset upload;
built-in training with ONNX download; a custom `model.py`; and node removal
with the key deleted from the worker.

The dashboard suite covers, as three users on three ports: each port keeping
its own sign-in; malformed, forged and expired tokens rejected; onboarding
through the form, including a wrong password; users not seeing each other's
workers, runs and datasets; health check, drain and resume; parallel runs with
per-task output; failure with the extracted error and retry of failed tasks;
cancel; dataset upload, training and ONNX download; worker removal; and sign
out. In every browser and phone: all pages render with no console errors,
deep links and unknown URLs, navigation at that screen size, no sideways
overflow, an axe accessibility scan with no serious problems (WCAG 2 A/AA),
"Offline" when the gateway is down, the sign-in dialog when the server
requires it, keyboard sign-in, a real run, and the security headers
(CSP, frame, referrer, content-type) on pages, assets and API responses.

Testing found and fixed: no way to sign in on the website (production mode was
unusable), a wrong SSH password treated as an expired sign-in, low-contrast
text, unlabelled form fields, broken layouts on phones, missing security
headers on most responses, a Google Fonts request on every page, and HDFS
containers being killed for lack of memory (heaps now capped; restarts
automatic; HDFS is optional since 2.0.0).

## 2. Before release (blocking)

1. **Push and get CI green.**
   ```bash
   git push origin main
   ```
   All jobs must pass: `python`, `spark-ray`, `frontend` and `extension`
   (Linux, macOS, Windows each), `go`, `e2e`. This is the first run of the OS
   matrices and the `e2e` job; fix anything they find before going further.
   Then protect `main` (Settings → Branches) so these checks are required.
2. **Marketplace publisher** `aaron43210`: done (1.0.0 was published with
   it on 2026-10-17).
3. **Publishing token.** Use the Azure DevOps Personal Access Token (scope
   *Marketplace → Manage*) that published 1.0.0, or create a new one. Store it
   as the repository secret `VSCE_PAT`; never commit it.
4. **Optional: Open VSX** (VSCodium, Cursor, Windsurf, Gitpod users). Create
   the namespace `aaron43210` at <https://open-vsx.org>, get a token, store it
   as `OVSX_PAT`.
5. **Prepare the repository for the public.** Turn on *Private vulnerability
   reporting* (Settings → Code security), which `SECURITY.md` relies on. Add a
   description and topics (ssh, distributed-computing, vscode-extension,
   machine-learning, docker). Check `LICENSE` (MIT, all three authors) is what
   you intend for the whole repository.
6. **Review the listing.** The Marketplace shows `README.md`, `CHANGELOG.md`,
   the icon and the description. Check the README renders, all links are
   absolute (`https://`), and no example uses real hosts or passwords other than
   the documented test workers.

## 3. Release

1. **Tag the platform release.** The extension downloads the platform
   release with its own version (`v2.0.0`), so this tag is what new users get.
   Without it they get the default branch.
   ```bash
   git tag -a v2.0.0 -m "Distributed AI Compute 2.0.0"
   git push origin v2.0.0
   ```
   Check: `git ls-remote --tags origin v2.0.0` shows the tag.
2. **Build the `.vsix` from the tag**, or take the `distributed-ai-compute-vsix`
   artifact from that commit's CI run. Always use `npm run package`: it points
   the README's images and links at
   `…/raw/main/distributed-ai-platform/distributed-compute-vscode/`, so the
   images must already be on `main` (step 2.1).
   ```bash
   cd distributed-ai-platform/distributed-compute-vscode
   npm ci && npm run package -- --out distributed-ai-compute-2.0.0.vsix
   ```
3. **Publish.**
   ```bash
   npx vsce publish --packagePath distributed-ai-compute-2.0.0.vsix -p "$VSCE_PAT"
   npx ovsx publish distributed-ai-compute-2.0.0.vsix -p "$OVSX_PAT"   # optional
   ```
   Check: within minutes the listing at
   `https://marketplace.visualstudio.com/items?itemName=aaron43210.distributed-ai-compute`
   shows version 2.0.0 and every screenshot loads.
4. **GitHub Release** for `v2.0.0`: attach the extension's 2.0.0 `.vsix`, paste the 2.0.0
   section of the extension's `CHANGELOG.md`, add screenshots from `frontend/docs/images/` and the extension's `media/readme/`, and list
   the known limits from section 7.

## 4. After release: smoke test on each OS

On one clean Windows, macOS and Linux machine each, with Docker installed:

1. Install the extension from the Marketplace.
2. Accept **Set Up Now**. It should start Docker if needed, download `v2.0.0`,
   build, start and connect. The first build takes several minutes.
3. **Add Compute Node** with a real second machine on the network.
4. **Run File in Parallel…** with a short script and 4 inputs; the output
   view should fill in live.
5. **Submit Training Job** with a small CSV; download the model.
6. **Remove** the node; its `authorized_keys` line must be gone.
7. Open the dashboard (http://localhost:3001) in that machine's browsers and
   on a phone on the same network (start with `BIND_ADDR=0.0.0.0 make up`
   for the phone): add a worker, run a task, train a model.

Record the results in the GitHub Release. Any failure becomes 2.0.1.

## 5. How users deploy the platform

**A. One computer (default).** Nothing to configure. The extension's setup
(or `make up`) uses local defaults: `ENVIRONMENT=development`, automatic
sign-in, every port bound to `127.0.0.1`.

**B. A small team on one network.** On the master machine:

1. In `config/.env`, set `ENVIRONMENT=production`, and set `JWT_SECRET`
   and `SSH_CRED_KEY` to long random strings:
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
   The services refuse to start with placeholders.
2. Put HTTPS in front of the dashboard (port 3001) and the extension
   gateway (port 8090), for example with Caddy or nginx and a certificate.
   SSH passwords for onboarding travel in request bodies. The extension
   accepts plain `http://` only for local and LAN addresses.
3. Start with `BIND_ADDR=0.0.0.0 make up` and open only the HTTPS ports in
   the firewall.
4. Give each person their own token:
   `python generate_token.py --sub <name> --hours 720`. On the dashboard they
   click the account button (top right) and paste it; in VS Code they run
   **Configure Master URL** and **Configure Authentication**. Each user sees
   only their own workers, runs and data, on both.

**C. Workers.** Each needs only `sshd` and `python3`, plus a dedicated
non-admin account. Nothing is installed on them. Tasks run with that
account's permissions, so never use an admin account.

## 6. Upgrade from 1.x, and rollback

**Upgrade.** Datasets uploaded before 2.0.0 are in HDFS. Nothing is lost by
upgrading: those rows keep pointing at HDFS, and new uploads go to local
files.

```bash
make backup             # database + dataset files to backups/<date>/
git pull                # or check out v2.0.0
make up                 # rebuilds the changed images and starts without HDFS
make migrate-datasets   # starts HDFS, copies each dataset, stops HDFS again
```

Until `make migrate-datasets` has run, downloading or training on an old
dataset answers 503 with "This dataset is stored in HDFS, which is not
running…". The migration copies one dataset at a time, checks its size, then
switches its row; a failed dataset stays on HDFS and is reported, and the
command can be re-run safely. It leaves the HDFS copies in place and prints
the `docker volume rm` command for this install's HDFS volumes once you have
checked your data. Installs that want to keep HDFS start with `make up-hdfs`
instead and skip the migration.

Tested: a 1.0 stack with datasets upgraded with exactly these commands; both
datasets downloaded byte for byte afterwards with HDFS stopped.

**Rollback.**

- **Extension:** `npx vsce unpublish` is a last resort. Prefer publishing
  2.0.1 with the fix; users can also pick *Install Another Version…* in VS Code.
- **Platform:** `make down`, check out the previous tag, `make up`. Data
  volumes stay. 1.x reads datasets only from HDFS, so after
  `make migrate-datasets` also restore `database.sql` from the backup taken
  before the upgrade: its rows point at the HDFS copies, which the migration
  kept. Datasets uploaded on 2.0.0 are then not visible in 1.x.
  `make reset` deletes every volume; use it only if a schema change went
  wrong. Schema changes are additive (`ADD COLUMN IF NOT EXISTS`).

## 7. Known limits to state in the release notes

- Windows **worker** machines are implemented but not verified on a real
  Windows host. The extension itself is tested on Windows by CI.
- Not yet tested against physical remote workers; only the Docker test
  workers.
- The sandbox isolates files and processes, not the kernel. Memory slices are
  a scheduling figure, and cores are not pinned on macOS.
- The first setup builds every image from source (several minutes, a few GB
  of disk). The running stack needs about 0.6 GB of memory for Docker
  (about 2 GB with HDFS).
- Spark + Ray is optional and adds about 1 GB on the master.

## 8. Next (2.1.0)

- Publish prebuilt images to GitHub Container Registry so setup pulls instead
  of building.
- Run the `e2e` job against a real Windows worker and a remote Linux machine.
- A client event stream (WebSocket) instead of polling.
- A real database migration tool.
- Sign-in with an identity provider (OIDC) instead of pasted tokens.
