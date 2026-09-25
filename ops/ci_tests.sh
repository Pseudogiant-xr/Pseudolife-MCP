#!/usr/bin/env bash
# Keep the full Linux suite and its exit status while recording runner evidence.
set -euo pipefail

mkdir -p ci-results
python - <<'PY' > ci-results/runtime.json
import importlib.metadata
import json
import os
import platform

# Explicit allowlist: never dump the runner environment or connection settings.
print(json.dumps({
    "python": platform.python_version(),
    "cpu_count": os.cpu_count(),
    "environment": {name: os.environ.get(name) for name in (
        "ImageOS", "ImageVersion", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE",
        "PSEUDOLIFE_REQUIRE_TEST_POSTGRES", "PSEUDOLIFE_TEST_EMBEDDER",
    )},
    "packages": {name: importlib.metadata.version(name) for name in (
        "pytest", "pytest-xdist", "torch", "sentence-transformers",
        "transformers", "psycopg", "numpy",
    )},
}, indent=2))
PY
lscpu > ci-results/cpu.txt
stat -f -c '%T' . /tmp > ci-results/filesystems.txt
snapshot() {
    for path in /proc/pressure/cpu /proc/pressure/io /proc/pressure/memory \
        /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.max; do
        if [[ -r "$path" ]]; then
            printf '\n%s\n' "$path"
            cat "$path"
        fi
    done
}
snapshot > ci-results/pressure-before.txt
free -m > ci-results/memory-before.txt
# Five-second samples distinguish sustained CPU work from paging / I/O waits.
vmstat -w 5 > ci-results/vmstat.txt &
monitor_pid=$!
cleanup() {
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
}
trap cleanup EXIT

# No pipeline: a failing pytest must remain a failing job. GNU time waits for
# pytest and records its CPU, peak RSS, faults, context switches and exit code.
# `timeout` ends a hung run inside the step (exit 124), so the step log
# survives and the always() diagnostics upload still runs. When the job
# timeout cancelled the hung 2026-09-22 run, GitHub kept neither. SIGABRT,
# not TERM: pytest enables faulthandler in the controller and every xdist
# worker, so each one prints all its thread stacks as it dies, including a
# hang outside any single test. 35 min: pytest took 11.8-15.5 min in the
# four green master runs of 2026-09-23/24, and install plus a cold
# embedder download (up to ~10 min) must still fit inside the job's 50.
set +e
/usr/bin/time -v -o ci-results/process.txt \
    timeout --signal=ABRT --kill-after=60s 35m \
    python -m pytest -q -n 2 --dist loadfile -ra \
    --durations=50 --durations-min=1 --junitxml=ci-results/junit.xml "$@"
test_status=$?
set -e
free -m > ci-results/memory-after.txt || true
snapshot > ci-results/pressure-after.txt || true
exit "$test_status"
