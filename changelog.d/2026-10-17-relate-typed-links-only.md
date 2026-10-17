### Fixed (2026-10-17 — the merge judge stops filling the link queue with untyped related-to proposals)
- Since 2026-09-30 an automatic merge reject whose vote names a relation
  files that relation as a link proposal. On the live bank on 2026-10-17,
  381 of the 435 pending link proposals came from that path, 274 of them the
  untyped `related-to`, and the link judge reached its 0.8 gate on none of
  the 435, so they only accumulated in the review queue. The path now files
  typed relations only (`part-of`, `implements`, `uses` and the rest);
  a `related-to` vote still rejects the merge but files nothing.
