### Added (2026-10-24 — self-hosted parity runners set up by script, with a watcher)
- `ops/selfhosted/setup-linux-runner.sh` sets up parity runners on a Linux dev box: a dedicated account with rootless Docker, no access to the local network, several runners each with its own home, and Python 3.11 in the runner tool cache.
- `ops/selfhosted/runner_watch.py`, installed as a timer by `ops/selfhosted/setup-runner-watch.sh`, keeps `PARITY_SELFHOSTED_RUNNERS` listing only machines with an online runner, so a machine that goes down stops receiving jobs that would queue for a day.
