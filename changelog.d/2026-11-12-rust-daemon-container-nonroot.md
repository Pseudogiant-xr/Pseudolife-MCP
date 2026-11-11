### Security (2026-11-12 — the Rust daemon container no longer runs as root)

- Since 0.23.0 the default Rust daemon image ran the daemon as root, keeping
  the `CHOWN`, `DAC_OVERRIDE`, `SETUID` and `SETGID` capabilities Compose
  grants only for the ownership bootstrap. Its entrypoint now does what the
  Python image's does: it transfers root-owned entries in `/data` and the
  model cache `/models` to UID/GID `10001:10001` (no symbolic links or other
  mounts are followed), then drops root, supplementary groups and every
  effective and permitted capability with `setpriv` before it provisions
  models and starts the daemon. Existing hosts get it at their next
  `pseudolife-mcp update`; files earlier root-run daemons, restores or
  update's model provisioning left are transferred on that first start, and
  an untransferable volume (for example a read-only one holding root-owned
  files) stops the start with a message naming the directory. The Rust
  shadow and mirror, which already run as `10001:10001`, are unchanged.
