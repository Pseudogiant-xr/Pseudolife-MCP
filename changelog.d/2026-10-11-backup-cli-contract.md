### Fixed (2026-10-11 — Backup CLI inputs and archive ownership)

- The native backup command accepts argparse option spellings and pathlib
  paths, including repeated options and parent components, and formats help
  for the terminal width. Output containment checks still prevent archives
  from including their own partial files.
  Windows containment also checks directory identity across local UNC aliases;
  help on a closed stdout uses Python's exit status.
- State archives retain Unix owner and group names, including non-UTF-8 NSS
  names through binary PAX records, alongside numeric IDs.
  PostgreSQL client discovery failures preserve Python's final OS diagnostic
  and output-directory creation; native output omits interpreter frames.
  Discovery paths with non-UTF-8 bytes and terminal widths beyond Python's
  integer conversion limit defer before effects.
