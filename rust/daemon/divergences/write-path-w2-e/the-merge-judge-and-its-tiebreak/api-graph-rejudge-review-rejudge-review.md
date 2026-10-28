| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `/api/graph/rejudge` (`review_rejudge`, `review_judgments.requeue`) | served | not served: the daemon serves no `/api/graph/*` route | the web API slice; `requeue` also reads the curation and candidate queues (task P3b) |
