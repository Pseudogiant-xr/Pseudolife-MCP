| Item | Python | Rust | Why / owner |
|---|---|---|---|
| One-time import of a pre-v57 chromadb store | at service start (`storage/migrate_chroma.py`) | none | D5 (2026-10-09): Chroma is dropped from the Rust daemon; the Python daemon's v57 start imports the store before a cutover |
