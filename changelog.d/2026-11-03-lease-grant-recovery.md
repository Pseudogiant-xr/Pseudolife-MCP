### Fixed (2026-11-03 — Lease queues recover missed turns)
- Both engines skip queued sessions with neither a recent attachment heartbeat nor a recent poll, retaining their queue position for recovery. A returning session counts as alive in its own lease request.
- A missed queue grant gets one retry behind the next live holder and before the remaining queue. Direct acquisitions, renewed grants and a second missed grant expire normally.
- Committed grants now produce one daemon notice with a lease-grant wake. Delivery retries after startup or failure, checks the current holder, fence and expiry, and deduplicates across daemon restarts. A cold service does not block heartbeats to initialize notification delivery; notices grant no user approval.
- Accepted grants do not generate redundant notices. Outbox pagination advances past refused recipients, and each pending refusal logs once per process. Maintainer, conductor and lease-grant wakes share the same exclusions from peer budgets.
