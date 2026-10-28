| Item | Python | Rust | Owner / why |
|---|---|---|---|
| The order of seed-search and re-query hits | `CMS.retrieve`'s ranking | `search::rank` | the search rows above: equal scores under `torch.topk` are free, and embeddings agree within the measured tolerance, so near-equal hits can swap and with them `texts` and the mention counts behind the re-query order |
