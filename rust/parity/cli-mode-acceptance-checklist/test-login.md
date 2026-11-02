| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| test-login | 5 | ported-with-substitution | native | Native admin-URL and Postgres-container psql creation retained; top-level/create help implemented. The login file stays on the invoking host, so this host operation does not rerun inside the daemon container. All 51 Linux non-Docker cases matched and native Windows contracts passed; both-platform goldens (including seven real Linux container cases) and the image proof passed; hosted stress remains pending. |
