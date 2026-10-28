| Item | Python | Rust | Owner / why |
|---|---|---|---|
| An embedder refusing a claim or digest text with a lone surrogate | the tokenizer raises `TypeError`: `TextEncodeInput must be Union[TextInputSequence, Tuple[InputSequence, InputSequence]]` | the same `str(exc)`, computed, not raised | the text is pinned to tokenizers 0.22.2 on CPython 3.11.9 (probed on the fixture's `tokenizer.json`, raw and through `AutoTokenizer`); another tokenizers release may word it differently |
