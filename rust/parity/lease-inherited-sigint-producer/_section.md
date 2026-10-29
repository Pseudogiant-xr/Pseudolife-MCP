## Lease inherited SIGINT producer contract

POSIX background commands started by a non-interactive shell, including
`ops/remote-suite.sh` and commands launched under `nohup`, can inherit
`SIGINT = SIG_IGN`. That disposition is an admitted producer input.
The pinned CPython 3.11.17 oracle's
[`signal_get_set_handlers`](https://github.com/python/cpython/blob/v3.11.17/Modules/signalmodule.c)
reads the inherited dispositions and installs `default_int_handler` only
when SIGINT was `SIG_DFL`; an inherited `SIG_IGN` stays ignored. Python's
`lease_cli.py::_install_stop_handlers` changes only SIGHUP and SIGTERM.

The native substitution for Python startup and `KeyboardInterrupt` uses an
OS disposition query before Tokio SIGINT registration: an inherited ignore
keeps SIGINT ignored, while a normal disposition retains the existing
interrupt cleanup and exit 130. SIGHUP and SIGTERM retain their existing
handlers. `shim/tests/lease_sigint.rs` supplies separate actual-binary
ignored and default-disposition controls with child and lock cleanup checks.
This source change supplies no acceptance receipt or parity-row promotion;
merged-runtime recapture of both A21 dispositions remains required.
