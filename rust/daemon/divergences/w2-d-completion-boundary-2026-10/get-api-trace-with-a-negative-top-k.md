| Named divergence | Omitted surface | Reason / owner |
|---|---|---|
| `GET /api/trace` with a negative `top_k` where the pool is empty (an empty bank, or a filter that leaves no candidate), or a `top_k` past i64 | `band.retrieve` returns before `torch.topk` when no entry is eligible, so the trace answers 200 with `config.top_k` as given and Python's negative-slice cuts; an unbounded `top_k` is echoed in `config.top_k` as written | a negative `top_k` is torch's refusal (500) whatever the pool; one past i64 saturates in the echo | non-canonical input, as the row above |
