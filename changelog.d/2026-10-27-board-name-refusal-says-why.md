### Fixed (2026-10-27 — a refused board name or label says which rule it broke)
- `memory_agents(action="update", name=...)` with a name such as "Rust
  B2a/B2d maintainer/config" answered only "invalid_name: the call was
  refused.", so an agent could not tell why. The refusal now names the cause
  and the fix: "the name contains the reserved word 'maintainer' (ignoring
  case, spacing, punctuation and look-alike letters); choose a name without
  it", or that it "reads as" a reserved word when it spells one. A name
  holding a zero-width or bidi character says so. A refused label says the
  same for labels. The refusal code and which names are refused are
  unchanged, on the Python and Rust daemons alike.
