### Changed (2026-10-20 — `-q` or `-n 2` no longer turns the WSL full suite into a one-process run)

- `ops/wsl-suite.sh` (and `ops/wsl-suite.ps1` / `ops/remote-suite.ps1`, which run it) keeps the full suite on its automatic xdist workers when the arguments are only display options (`-q` to `-qqqq`, `-v` to `-vvvv`, `--quiet`, `--verbose`, `-r<chars>`, `--durations`, `--durations-min`, `--tb`). These replace the default `-q -rs`.
- A worker choice (`-n N`, `-nN`, `--numprocesses`; a whole number, `auto` or `logical`) or a `--dist` mode now wins over the automatic choice instead of being added beside it. `--dist no` alone runs the suite in one process.
- Before this change, any argument replaced the whole default, so `-q` alone ran the suite in one process.
- Paths, node ids, `-k`, `-m`, collection and help modes, unknown options, and any `--` still go to pytest unchanged, with nothing added, and pytest judges them. The run says so ("no xdist workers added").
- In a full-suite run, a worker or display option missing its value, a worker choice given twice, or an invalid count, distribution or number exits 2 before anything is fetched or installed.
- The run prints its choice and where it came from (`full suite, 4 xdist workers (automatic)`, `(-n)`, `(--dist no)` or `(PSEUDOLIFE_SUITE_WORKERS)`), and for a full-suite run the exact pytest command it built.
- `ops/integrate.py open` still accepts only a receipt with no arguments as full-suite evidence.
