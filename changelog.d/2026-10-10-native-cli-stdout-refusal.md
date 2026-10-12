### Fixed (2026-10-10 — native connect, export, import and tunnel exit 120 on a refused stdout)

- The experimental native `connect`, `export`, `import` and `tunnel` now exit
  120 when stdout refuses their report (a closed pipe), as Python does: its
  buffered prints succeed, the run completes, and the interpreter's final
  flush fails with exit 120. They used to return their ordinary code
  (`connect`), or 1 (`export`, `import`, `tunnel`). Every step still runs in
  the same order, so files and requests are unchanged.
