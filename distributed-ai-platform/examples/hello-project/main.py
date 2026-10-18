"""The smallest project: a module of its own, an input per task, an output file."""
import json
import os
import platform
import sys

from src.greet import greet

name = sys.stdin.read().strip() or "world"
message = greet(name)
print(message, "from", platform.node())

out = os.environ.get("DC_OUTPUT_DIR", "outputs")
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "greeting.json"), "w") as fh:
    json.dump({"name": name, "message": message, "task": os.environ.get("DC_TASK_INDEX")}, fh)
