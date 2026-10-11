### Fixed (2026-10-10 — Keep wait-mail interruptible during FIFO replacement)

- On Linux, native `wait-mail` observes Ctrl-C even when the digest is replaced
  by a FIFO and the signal arrives just before an open or read. Nonblocking reads
  with bounded readiness waits preserve complete delivery and listener cleanup.
