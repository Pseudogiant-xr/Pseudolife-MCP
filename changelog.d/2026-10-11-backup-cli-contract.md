### Fixed (2026-10-11 — Backup CLI inputs and archive ownership)

- The native backup command accepts argparse option spellings and pathlib
  paths, including repeated options and parent components, and formats help
  for the terminal width. Output containment checks still prevent archives
  from including their own partial files.
- State archives retain Unix owner and group names alongside numeric IDs.
  PostgreSQL client discovery failures preserve Python's final OS diagnostic
  and output-directory creation; native output omits interpreter frames.
