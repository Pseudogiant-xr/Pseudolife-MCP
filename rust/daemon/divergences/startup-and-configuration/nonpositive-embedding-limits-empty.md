| Item | Python | Rust | Why |
|---|---|---|---|
| Nonpositive embedding limits / empty tokenization | backend-specific Python errors | batch_size and max_seq_length <= 0 refuse; a zero-token batch reports `empty tokenization` | unsupported input/limit shapes are explicitly refused before invalid inference |
