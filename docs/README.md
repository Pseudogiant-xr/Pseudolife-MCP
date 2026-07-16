# docs/

Two kinds of content live here. **`guide/` is user-facing documentation** —
the deep material behind the top-level [README](../README.md) (which stays
the short front door and PyPI description). Everything else is internal
design history.

| Directory | What's in it |
|---|---|
| `guide/` | **User-facing guides**: [configuration](guide/configuration.md), [retrieval](guide/retrieval.md), [dreaming](guide/dreaming.md), [episodes & sessions](guide/episodes.md), [the memory model](guide/memory-model.md), [benchmarks](guide/benchmarks.md) |
| `specs/` | Design documents for shipped features (problem → decision → shape) |
| `plans/` | Implementation plans derived from those specs |
| `runbooks/` | Operational procedures for the live deployment |
| `superpowers/` | Older specs/plans written under a plan-review workflow of that name — same kind of content as `specs/` + `plans/`, kept where their inbound links expect them |
| `images/` | README assets |

Top-level files are point-in-time investigation reports (e.g. the neural-memory
investigation that led to the v0.5 cosine spine). They describe the state of
the project *when written* and are kept for the paper trail, not maintained.
The `guide/` pages, by contrast, **are** maintained — they're part of the
release docs-currency pass alongside the README.
