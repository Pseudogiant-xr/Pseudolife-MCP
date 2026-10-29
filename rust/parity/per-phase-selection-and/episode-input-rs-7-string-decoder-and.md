| Episode site | Producer/input class | Substitution or retained reason |
|---|---|---|
| `episode_input.rs:7` string decoder and `:107` quoting | User stdin / hook strings and actual OS cwd spelling | Keep serde_json byte-string decoding plus a minimal WTF-8 codepoint adapter for escaped host surrogate units; ordinary JSON forbids raw string controls. ASCII JSON escaping and request-body spacing remain exact. This is a filesystem/string codec, not a general interpreter value model. |
