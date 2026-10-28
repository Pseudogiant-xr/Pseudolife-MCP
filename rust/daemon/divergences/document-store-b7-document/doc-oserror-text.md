| Item | Python | Rust | Why / owner |
|---|---|---|---|
| **DOC-OSERROR-TEXT:** an OSError's text on Windows (`Cannot read file {name}: {error}`, and a file removed between the existence check and the read) | `[Errno N] strerror: 'path'` from the C runtime's errno (reading a directory is `[Errno 13] Permission denied`) | `[Errno W] <FormatMessage text>: 'path'` with the Win32 code (`[Errno 5] Access is denied.`) | both the number and the words differ on Windows; Linux and macOS render `[Errno N] strerror: 'path'` as Python does |
