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
# hang outside any single test. 50 min (issue #1408): the `test` job's
# Tests step took 31.9 min (master push 43456549451), 35.0 (batch
# 43495728956) and 25.5 (batch 43476091008) on 2026-11-13/14, 2 xdist
# workers on a 4-CPU ubuntu-latest runner with the real embedder, and the
# old 35-minute bound killed batch 43525635914 at 99%; 50 is about 40% over
# the slowest. Install plus a cold embedder download (up to ~10 min) and
# the kill grace must still fit inside the job's 65 (tests/
# test_hang_diagnostics.py). test-lite-linux shares the bound; its step
# took 17.9-23.8 min on the same runs.
# ops/pytest_failure_lines.py prints `FAILED <node id> - <reason>` the moment
# a test fails, so a run killed before its summary still names it, and
# appends each test phase's seconds to ci-results/test-times.tsv, so a
# killed run still says where its time went (--durations prints at the end).
# PSEUDOLIFE_SUITE_PROFILE=1 also loads tests/suite_profile_plugin.py (worker
# timeline, resource and test-server samples, run manifest) into
# ci-results/profile and folds its summary; the selection, workers and exit
# status are unchanged. Off by default until its overhead is measured.
profile=()
case "${PSEUDOLIFE_SUITE_PROFILE:-}" in
    ""|0|off|false|no) ;;
    *)
        export PSEUDOLIFE_SUITE_PROFILE_DIR="$PWD/ci-results/profile"
        profile=(-p tests.suite_profile_plugin)
        ;;
esac
export PSEUDOLIFE_CI_TEST_TIMES="$PWD/ci-results/test-times.tsv"
: > "$PSEUDOLIFE_CI_TEST_TIMES"
set +e
/usr/bin/time -v -o ci-results/process.txt \
    timeout --signal=ABRT --kill-after=60s 50m \
    python -m pytest -p ops.pytest_failure_lines ${profile[@]+"${profile[@]}"} -q -n 2 --dist loadfile -ra \
    --durations=50 --durations-min=1 --junitxml=ci-results/junit.xml "$@"
test_status=$?
if (( ${#profile[@]} )) && [[ -f ci-results/profile/manifest.json ]]; then
    python evals/suite_profile_summary.py --profile-dir ci-results/profile \
        --tag ci --out ci-results/profile-summary.json \
        || echo "ci_tests: the profile summary failed" >&2
fi
set -e
free -m > ci-results/memory-after.txt || true
snapshot > ci-results/pressure-after.txt || true
exit "$test_status"
