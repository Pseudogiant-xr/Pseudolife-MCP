### Added (2026-10-21 — CI flags a dependency lock the daemon image cannot install)
- A new CI job, `lock-resolve`, replays the daemon image's constrained
  install (`ops/Dockerfile.daemon` step 2: pyproject's dependencies and the
  onnx extra under `pip install -c ops/requirements.lock.txt`) as a pip dry
  run on the image's Python, using `ops/check_lock_resolves.py`. A lock whose
  pins contradict each other now goes red on its pull request, showing pip's
  conflict lines, instead of breaking the image build after merge.
  Dependabot's #875 was the case: aiohttp 3.14.4 requires `yarl>=1.25.1`, the
  lock still pinned yarl 1.24.2, and every other job was green because none
  resolves the lock. It blocks a merge once the ruleset requires it; the
  merge-queue spec now lists it among the checks to require.
- Run it locally in the image's base, from the checkout (bash or PowerShell;
  Git Bash needs `MSYS_NO_PATHCONV=1` in front):
  `docker run --rm -v "${PWD}:/src:ro" -w /src python:3.12-slim python ops/check_lock_resolves.py`.
  It refuses, checking nothing, on any other OS or Python version, because
  environment markers can resolve differently there.
