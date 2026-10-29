### Security (2026-10-28 — setuptools 84.2.1 and filelock 4.2.1 in the daemon image)
- The daemon image's pinned dependencies (`ops/requirements.lock.txt`) move
  setuptools from 83.0.0 to 84.2.1 (Dependabot alert #60, low: a Windows
  path escape through a wheel's `namespace_packages.txt` during legacy wheel
  installation, GHSA-xmj3-9gf7-h52w), in the lock and in
  `ops/Dockerfile.daemon`'s explicit pin. torch 2.13.0 requires only
  `setuptools>=77.0.3`, so the CPU torch pin is unaffected.
- filelock moved from 3.29.0 to 4.2.1 in Dependabot's #923 (2026-10-26),
  which had no changelog entry (alerts #61, high, and #62–#63, low: another
  local user who can create files beside a `ReadWriteLock`'s SQLite database
  can make the lock user's process overwrite or unlink its own files,
  GHSA-v4x8-3wr5-3wq8, or on Windows act on side files placed there,
  GHSA-9vhv-jxww-xfm5; on Windows an oplock on a soft lock's marker holds
  `SoftFileLock.acquire()` past its timeout, GHSA-567q-5qvw-pf92). filelock
  reaches the image only through torch, huggingface_hub and transformers,
  none of which uses a read-write lock, and the image runs on Linux.
- Dependabot's `setuptools >=82` ignore rule is retired: its own lift
  condition (a pinned torch with no setuptools ceiling) was met on
  2026-08-02, and since then it has kept Dependabot from proposing
  setuptools security fixes such as #60's.
- Users get the new image with the next release, through `pseudolife-mcp
  update`; nothing to do by hand.
