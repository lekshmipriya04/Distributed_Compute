# Changelog

All notable changes to the Distributed Compute extension are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.2] - 2026-10-17

### Added
- **Run File in Parallel…** and **Run Python File** in the editor's Run
  (play button) menu.

### Changed
- The extension no longer depends on the web dashboard: **Open Dashboard**
  and the `dashboardUrl` setting are gone; the status menu offers **Add
  Compute Node** instead.
- The platform download uses the platform release this extension was tested
  with (`v0.1.0`) instead of whatever the default branch holds.
- Clearer views: runs are named "python · 8 tasks" instead of by ID, workers
  show "online · 2 of 12 cores", statuses read "completed with errors", and
  the status bar shows "2/2 nodes online · no jobs running" and stays current.
- Empty views show what to do next, with buttons (Set Up Platform, Add
  Compute Node, Run File in Parallel, Submit Training Job).
- Commands use a category, so menus show short names ("Run Python File").
- Removed the unused `enableCodeLens` setting.

### Fixed
- Windows: Docker Desktop is started correctly when it is not running.
- Linux: a "permission denied" from Docker explains how to join the `docker`
  group instead of waiting three minutes; systems with only `docker-compose`
  (Compose v1) work.
- Files saved with Windows line endings no longer break shell scripts on
  Linux workers.
- Start-up no longer waits for an answer to the "set up the platform?"
  notification.
- Running a file right after another run no longer sends the previous run's
  output to the workers: output opens beside the code without taking focus,
  and only files (or unsaved editors) are ever run.
- The status bar counts stayed at their values from connecting.
- The Run (play button) entry showed its full title instead of an icon.
- Detected languages no longer repeat their name ("python Python 3.14.8").

### Security
- Restricted Mode (untrusted workspaces) is supported: the master URL and
  platform folder from workspace settings are ignored, and a
  `docker-compose.yml` inside the workspace is never run. `platformPath` can
  only be set per machine.

## [1.0.0] - 2026-10-17

First public release. Works on Windows, macOS and Linux (VS Code 1.90+);
the platform itself runs in Docker.

### Added
- Everything from the editor, no dashboard needed: **Add Compute Node** (host
  key check, one-time password or manual key command, detection, slice),
  **Test Node Connection**, **Detect Node Hardware Again**, **Rotate Node
  Key**, **Drain** / **Resume**, **Remove Node**.
- **Run File in Parallel…**: one task per input, per file line, or N copies,
  on chosen nodes with chosen cores per task.
- Live per-task output for every run; **Cancel**, **Retry** (all or only
  failed tasks) and **Save Execution Results**.
- One-click local setup: **Set Up Platform on This Computer** checks and starts
  Docker, finds or downloads the platform, creates `config/.env`, starts the
  containers and connects. It is offered on first start, in the Compute Nodes
  view and in a Get Started walkthrough. Later starts are automatic
  (`autoStartPlatform`). Also **Stop Platform on This Computer** and **Open
  Dashboard**.
- One-click sign-in: connecting to a master on this machine or your LAN in
  development mode gets a token automatically. An expired token is replaced on
  the next connect.
- `masterUrl` defaults to `http://localhost:8090`, the gateway started by the
  platform's `docker compose`.
- Training: **Submit Training Job** (dataset → engine → model → target →
  parameters), **Train on This Dataset** and **Train with This Model File**
  context menus, **Run Training Again** from `distributed.json`, **New Custom
  Model File**, and a **Training Runs** view with progress, metrics, details and
  model download.
- Custom models (your `model.py`, trained on your workers) and every Spark
  MLlib model, with grid search.
- **Enable / Disable Spark + Ray Engine**: installs the optional engine into a
  master on this machine, only when you ask.
- Settings: `training.defaultEngine`, `training.defaultRounds`,
  `training.defaultEpochs`, `training.defaultLearningRate`, `platformPath`.
- `distributed.json` accepts a `training` block (schema-validated).
- Error messages from the backend (for example why a training request was
  rejected) are shown as they are, instead of a generic message.

### Changed
- Commands the backend does not support yet are hidden from the Command Palette.

### Removed
- Spark and Ray run commands (`runSpark`, `runRay`) and the `spark`/`ray`
  runtime values: the backend removed Spark and Ray, and it rejected those
  runtimes as unknown.

### Added
- `Run File with Runtime…` lists the runtimes detected on your online nodes
  (`GET /runtimes`) and submits the active file with the one you pick.
  `defaultRuntime` and `distributed.json` accept any runtime id.
- Master connection management with URL validation and token stored in VS Code
  SecretStorage (never in settings or logs).
- Typed REST client for the extension gateway with timeouts, cancellation,
  request IDs, and normalized, secret-free error messages.
- Compute Nodes, Jobs, and Executions tree views backed by the gateway, with a
  status bar summary and a controlled refresh interval.
- Run commands that submit executions through the
  gateway; capabilities the connected backend does not expose report an honest
  "not supported" message instead of faking success.
- `distributed.json` project configuration: schema, `Initialize Project`, and
  `Validate Project Configuration`.

## [0.0.1] - 2026-10-09

### Added
- Phase-1 scaffold: activation, command registration, diagnostics output
  channel, status bar item, project schema.
