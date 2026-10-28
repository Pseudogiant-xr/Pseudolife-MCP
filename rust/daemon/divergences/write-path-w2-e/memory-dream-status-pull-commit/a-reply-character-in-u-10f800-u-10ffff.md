| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A reply character in U+10F800–U+10FFFF | written | taken as a lone-surrogate stand-in: the write fails | the reply parser keeps lone surrogates as private code points so a write fails where Python's encoding fails |
