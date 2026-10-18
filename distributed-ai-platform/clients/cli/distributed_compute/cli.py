"""dc: run folders of code on your Distributed Compute workers.

    dc run -- python train.py --epochs 3      run this folder, follow the output
    dc run -i 0.1 -i 0.01 -- python train.py   one task per input (stdin, $DC_INPUT_FILE)
    dc runs | dc logs ID | dc pull ID | dc cancel ID
    dc nodes | dc cache NODE 20
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .client import ApiError, Client
from .packing import pack, read_project_block, safe_extract

TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled", "timed_out", "node_lost"}
POLL_SECONDS = 2.0


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" or n >= 10 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} GB"


def _print_task(task: dict, names: dict) -> None:
    node = names.get(task.get("node_id") or "", task.get("node_id") or "not scheduled yet")
    exit_code = task.get("exit_code")
    exit_text = "" if exit_code is None else f" · exit {exit_code}"
    seconds = f" · {task['duration_ms'] / 1000:.1f}s" if task.get("duration_ms") else ""
    files = f" · {task['output_files']} output file(s)" if task.get("output_files") else ""
    print(f"━━ Task {task['seq'] + 1} · {task['status']}{exit_text}{seconds} · {node}{files}")
    if task.get("stdout"):
        print(task["stdout"].rstrip())
    if task.get("stderr"):
        print("── stderr")
        print(task["stderr"].rstrip())


def _node_names(client: Client) -> dict:
    try:
        return {n["id"]: n.get("name") or n["id"] for n in client.nodes()}
    except ApiError:
        return {}


def follow(client: Client, execution_id: str) -> int:
    """Print each task's output once it finishes; exit 0 only if every task succeeded."""
    names = _node_names(client)
    shown: set = set()
    last_status = None
    while True:
        data = client.tasks(execution_id)
        batch = data["batch"]
        if batch["status"] != last_status:
            message = f" · {batch['message']}" if batch.get("message") else ""
            print(f"[dc] {batch['status']}{message}", file=sys.stderr)
            last_status = batch["status"]
        for task in sorted(data["tasks"], key=lambda t: t["seq"]):
            if task["id"] not in shown and task["status"] not in ("pending", "running"):
                shown.add(task["id"])
                _print_task(task, names)
        if batch["status"] in TERMINAL:
            break
        time.sleep(POLL_SECONDS)
    ok = batch["status"] == "completed"
    with_files = sum(1 for t in data["tasks"] if t.get("output_files"))
    if with_files:
        print(f"[dc] {with_files} task(s) brought back output files: dc pull {execution_id}", file=sys.stderr)
    return 0 if ok else 1


def cmd_run(client: Client, args) -> int:
    folder = Path(args.directory).resolve()
    block = read_project_block(folder)
    command = " ".join(args.command).strip() or block.get("command", "")
    if not command:
        print("Give the command to run after --, for example: dc run -- python train.py", file=sys.stderr)
        return 2
    packed = pack(folder, block.get("exclude") or [])
    requirements = args.requirements or block.get("requirements")
    if requirements is None and "requirements.txt" in packed.files:
        requirements = "requirements.txt"
    inputs = list(args.input or [])
    if args.inputs_file:
        inputs += [ln for ln in Path(args.inputs_file).read_text("utf-8").splitlines() if ln.strip()]
    if args.copies:
        inputs += [str(i + 1) for i in range(args.copies)]
    datasets = [
        {"datasetId": d.split(":", 1)[0], **({"path": d.split(":", 1)[1]} if ":" in d else {})}
        for d in (args.dataset or [])
    ] or [{"datasetId": d["dataset"], **({"path": d["path"]} if d.get("path") else {})}
          for d in block.get("datasets") or []]
    print(f"[dc] {len(packed.files)} files, {_size(packed.bytes)}"
          + (f", environment from {requirements}" if requirements else ""), file=sys.stderr)
    if client.has_bundle(packed.sha256):
        print("[dc] unchanged since the last run, not uploaded again", file=sys.stderr)
    else:
        client.upload_bundle(packed.archive)
        print(f"[dc] uploaded {_size(len(packed.archive))}", file=sys.stderr)
    request = {
        "runtime": "project",
        "inputs": inputs,
        "project": {
            "bundleId": packed.sha256,
            "command": command,
            **({"requirements": requirements} if requirements else {}),
            **({"outputs": args.output} if args.output else
               ({"outputs": block["outputs"]} if block.get("outputs") else {})),
            **({"datasets": datasets} if datasets else {}),
        },
    }
    if args.node:
        request["nodeIds"] = args.node
    if args.cpu:
        request["cpuPerTask"] = args.cpu
    if args.timeout:
        request["timeoutSeconds"] = args.timeout
    execution = client.create_execution(request)
    print(f"[dc] started {execution['id']} · {execution['total']} task(s)", file=sys.stderr)
    if args.no_follow:
        print(execution["id"])
        return 0
    code = follow(client, execution["id"])
    if args.pull:
        cmd_pull(client, argparse.Namespace(id=execution["id"], dest=None))
    return code


def cmd_runs(client: Client, args) -> int:
    items = sorted(client.executions(), key=lambda e: e.get("createdAt", ""), reverse=True)[: args.limit]
    for e in items:
        what = (e.get("project") or {}).get("command") or e["runtime"]
        print(f"{e['id']}  {e['status']:<22} {e.get('completed', 0)}/{e['total']}  {what}")
    return 0


def cmd_logs(client: Client, args) -> int:
    if args.follow:
        return follow(client, args.id)
    data = client.tasks(args.id)
    names = _node_names(client)
    batch = data["batch"]
    print(f"Execution {batch['id']} · {batch['status']}" + (f" · {batch['message']}" if batch.get("message") else ""))
    for task in sorted(data["tasks"], key=lambda t: t["seq"]):
        _print_task(task, names)
    return 0


def cmd_pull(client: Client, args) -> int:
    data = client.tasks(args.id)
    dest = Path(args.dest or Path("distributed-results") / args.id)
    tasks = [t for t in data["tasks"] if t.get("output_files")]
    if not tasks:
        print("This run brought back no files. Tasks bring back what they write to outputs/ ($DC_OUTPUT_DIR).",
              file=sys.stderr)
        return 1
    total = 0
    for task in tasks:
        total += safe_extract(client.task_outputs(args.id, task["id"]), dest / f"task-{task['seq'] + 1:03d}")
    print(f"[dc] saved {total} file(s) from {len(tasks)} task(s) to {dest}", file=sys.stderr)
    return 0


def cmd_cancel(client: Client, args) -> int:
    print(f"[dc] {client.cancel(args.id)['status']}", file=sys.stderr)
    return 0


def cmd_nodes(client: Client, args) -> int:
    for n in client.nodes():
        cache = n.get("envCacheGb") or 0
        print(f"{n['id']}  {n.get('name') or n.get('host')}  {n['status']}  "
              f"{n.get('osType') or '?'}  {n.get('cpuAllocatable', '?')} cores  "
              f"env cache {f'{cache:g} GB' if cache else 'off'}")
    return 0


def cmd_cache(client: Client, args) -> int:
    node = client.set_env_cache(args.node, args.gb)
    print(f"[dc] {node.get('name') or node['id']}: environment cache "
          f"{f'{args.gb:g} GB' if args.gb else 'off'}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dc", description="Run folders of code on your Distributed Compute workers.")
    parser.add_argument("--version", action="version", version=f"dc {__version__}")
    parser.add_argument("--master", help="gateway URL (default $DC_MASTER or http://localhost:8090)")
    parser.add_argument("--token", help="sign-in token (default $DC_TOKEN; a local master signs you in itself)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run this folder on the workers")
    run.add_argument("command", nargs=argparse.REMAINDER, help="the command, after --")
    run.add_argument("-C", "--directory", default=".", help="folder to run (default: here)")
    run.add_argument("-i", "--input", action="append", help="one task per input (repeat)")
    run.add_argument("--inputs-file", help="one task per non-empty line of this file")
    run.add_argument("--copies", type=int, help="N tasks, each given its number 1..N")
    run.add_argument("-r", "--requirements", help="pip requirements file (default requirements.txt if present)")
    run.add_argument("-o", "--output", action="append", help="folder to bring back (default outputs)")
    run.add_argument("-d", "--dataset", action="append", help="dataset id[:path in the folder]")
    run.add_argument("-n", "--node", action="append", help="only these node ids")
    run.add_argument("--cpu", type=int, help="cores per task")
    run.add_argument("--timeout", type=int, help="seconds per task")
    run.add_argument("--no-follow", action="store_true", help="print the run id and return")
    run.add_argument("--pull", action="store_true", help="download the outputs when it finishes")
    run.set_defaults(fn=cmd_run)

    runs = sub.add_parser("runs", help="recent runs")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(fn=cmd_runs)
    logs = sub.add_parser("logs", help="output of a run")
    logs.add_argument("id")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(fn=cmd_logs)
    pull = sub.add_parser("pull", help="download a run's output files")
    pull.add_argument("id")
    pull.add_argument("dest", nargs="?", help="default distributed-results/<id>")
    pull.set_defaults(fn=cmd_pull)
    cancel = sub.add_parser("cancel", help="stop a run")
    cancel.add_argument("id")
    cancel.set_defaults(fn=cmd_cancel)
    nodes = sub.add_parser("nodes", help="your worker nodes")
    nodes.set_defaults(fn=cmd_nodes)
    cache = sub.add_parser("cache", help="lend disk on a node for cached environments (0 = off)")
    cache.add_argument("node")
    cache.add_argument("gb", type=float)
    cache.set_defaults(fn=cmd_cache)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    client = Client(args.master, args.token)
    try:
        return args.fn(client, args)
    except (ApiError, ValueError, OSError) as exc:
        print(f"dc: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
