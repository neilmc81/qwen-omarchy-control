#!/usr/bin/env bash
# Measure desktop-control latency over the MCP fast path (safe/read-only ops).
# Usage: bench-desktop.sh [count]
set -u
COUNT="${1:-5}"
ROOT="$(dirname "$(dirname "$(readlink -f "$0")")")"
export PYTHONPATH="$ROOT/src"

python3 - "$COUNT" "$ROOT" <<'PY'
import statistics, subprocess, sys, time

count = int(sys.argv[1])
root = sys.argv[2]
ops = ["get_active_window", "list_windows", "list_workspaces",
       "get_audio_status", "get_system_status"]
rows = []
for op in ops:
    times = []
    for _ in range(count):
        start = time.perf_counter()
        subprocess.run([root + "/bin/desktop-control", "run", op, "--json"],
                       capture_output=True, text=True, timeout=15)
        times.append((time.perf_counter() - start) * 1000)
    rows.append((op, statistics.median(times), min(times), max(times)))
print(f"desktop fast-path latency over {count} runs (ms)")
print(f"{'op':<28}{'median':>9}{'min':>9}{'max':>9}")
for op, med, lo, hi in rows:
    print(f"{op:<28}{med:>9.1f}{lo:>9.1f}{hi:>9.1f}")
allmed = statistics.median([r[1] for r in rows])
print(f"{'ALL OPS median':<28}{allmed:>9.1f}")
PY