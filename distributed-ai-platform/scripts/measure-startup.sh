#!/usr/bin/env bash
# Measures a fresh start of the platform: time until every service is healthy,
# time until the first dataset upload succeeds, memory in use, container count
# and image size. Runs under its own Compose project, so your data is never
# touched; that project's volumes are deleted afterwards (unless --keep).
#
#   scripts/measure-startup.sh [--keep] [extra docker compose args...]
#   e.g. scripts/measure-startup.sh --profile hdfs     (with STORAGE_BACKEND=hdfs)
#
# Stop your own stack first (make down): container names and ports are shared.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${MEASURE_PROJECT:-dcmeasure}"
KEEP=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--keep" ]; then KEEP=1; else ARGS+=("$a"); fi
done
compose() { docker compose -p "$PROJECT" ${ARGS[@]+"${ARGS[@]}"} "$@"; }
now() { python3 -c 'import time; print(time.time())'; }

[ -f config/.env ] || cp config/.env.example config/.env

echo "Building images (not timed)…"
compose build --quiet >/dev/null
compose down -v --remove-orphans >/dev/null 2>&1 || true

start=$(now)
compose up -d --wait --wait-timeout 600 >/dev/null 2>&1
healthy=$(now)

sample="$(mktemp)"
printf 'x,label\n1,0\n2,1\n' > "$sample"
for _ in $(seq 1 300); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -F name=measure -F "file=@${sample};filename=measure.csv" \
    http://127.0.0.1:8001/api/v1/storage/datasets || true)
  [ "$code" = "201" ] && break
  sleep 0.5
done
uploaded=$(now)
rm -f "$sample"
sleep 5   # let memory settle

containers=$(compose ps -q | wc -l | tr -d ' ')
memory_mb=$(docker stats --no-stream --format '{{.MemUsage}}' $(compose ps -q) | python3 -c '
import re, sys
units = {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}
total = 0.0
for line in sys.stdin:
    m = re.match(r"([\d.]+)(\w+)", line.split("/")[0].strip())
    if m:
        total += float(m.group(1)) * units.get(m.group(2), 0)
print(round(total))')
image_mb=$(compose images --format json | python3 -c '
import json, sys
seen, total = set(), 0
data = json.load(sys.stdin)
for img in data if isinstance(data, list) else [data]:
    key = img.get("ID") or img.get("Repository")
    if key not in seen:
        seen.add(key)
        total += int(img.get("Size", 0))
print(round(total / 1e6))')

python3 - "$start" "$healthy" "$uploaded" "$containers" "$memory_mb" "$image_mb" <<'EOF'
import sys
start, healthy, uploaded = map(float, sys.argv[1:4])
containers, memory, images = sys.argv[4:7]
print(f"containers running      {containers}")
print(f"start until healthy     {healthy - start:.1f} s")
print(f"start until 1st upload  {uploaded - start:.1f} s")
print(f"memory in use           {memory} MB")
print(f"images                  {images} MB")
EOF

if [ "$KEEP" -eq 0 ]; then
  compose down -v --remove-orphans >/dev/null 2>&1
fi
