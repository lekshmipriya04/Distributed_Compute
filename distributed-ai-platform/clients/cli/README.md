# dc: Distributed Compute from the terminal

Run a project folder on your Distributed Compute workers, follow its output,
and download what it wrote. It talks to the same gateway as the VS Code
extension (port 8090) and needs only Python 3.9+, with no other packages.

```bash
pip install ./clients/cli         # from distributed-ai-platform/
cd my-project                     # your code, requirements.txt, ...
dc run -- python train.py         # one task; output appears as tasks finish
```

## Run a folder

```bash
dc run -i 0.1 -i 0.01 -i 0.001 -- python train.py --epochs 3   # one task per input
dc run --inputs-file rates.txt -- python train.py              # one task per line
dc run --copies 8 -- python simulate.py                        # 8 tasks, inputs 1..8
dc run --pull -- python train.py                               # and download outputs at the end
```

- Each task gets its input on stdin and in `$DC_INPUT_FILE`, plus
  `$DC_TASK_INDEX` and `$DC_TASK_COUNT`.
- `requirements.txt` is installed on each worker when it is there; choose
  another file with `-r`.
- Anything a task writes to `outputs/` (`$DC_OUTPUT_DIR`) comes back. Choose
  other folders with `-o models -o logs`.
- Datasets uploaded to the platform can be copied into the folder:
  `-d <dataset id>:data/train.csv` (available as `$DC_DATA_DIR`).
- `-n <node id>` (repeatable) limits the run to some workers, `--cpu 4` gives
  each task 4 cores, `--timeout 1800` allows 30 minutes per task.
- `--no-follow` prints the run id and returns at once.

The folder is packed without `.git`, caches, virtual environments and
`outputs/`. In a git repository `.gitignore` applies as well. An unchanged
folder is not uploaded again.

Settings can live in the folder's `distributed.json`, which the VS Code
extension reads too:

```json
{
  "version": 1,
  "name": "my-project",
  "project": {
    "command": "python train.py",
    "requirements": "requirements.txt",
    "outputs": ["outputs"],
    "exclude": ["data/raw/", "*.ckpt"]
  }
}
```

With that file, `dc run -i 0.1 -i 0.01` is enough.

## Other commands

| Command | |
|---|---|
| `dc runs` | recent runs, newest first |
| `dc logs <id> [-f]` | a run's output (`-f` waits for it to finish) |
| `dc pull <id> [folder]` | each task's outputs, into `distributed-results/<id>/task-001/…` |
| `dc cancel <id>` | stop a run |
| `dc nodes` | your workers |
| `dc cache <node id> 20` | let a worker keep 20 GB of environments for later tasks (`0` turns it off) |

`dc run` exits with 0 only when every task succeeded, so it works in scripts
and CI.

## Which master, and signing in

`dc` uses `http://localhost:8090`. For another master set `DC_MASTER` (or pass
`--master`). A master on your own computer in development mode signs you in
by itself. Otherwise set `DC_TOKEN` to a token from
`python generate_token.py --sub <you>` (or pass `--token`).
