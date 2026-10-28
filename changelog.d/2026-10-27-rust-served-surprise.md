### Fixed (2026-10-27 — the Rust daemon logs a served memory's surprise as Python does)

- A retrieval event's served components on the Rust daemon carried the surprise of a memory stored in the running process rounded to 32-bit precision, where the Python daemon logs the value the store computed. They now carry that value. After a restart both daemons already logged the stored value.
