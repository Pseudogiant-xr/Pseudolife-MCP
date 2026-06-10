"""Storage backends (schema v8) — Postgres source of truth for the daemon.

The in-memory band / cortex structures remain the hot path; this package
is the durable write-through layer underneath them. See
``docs/specs/2026-06-10-v0.2-daemon-postgres-design.md`` §4.
"""
