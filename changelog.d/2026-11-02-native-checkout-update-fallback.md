### Fixed (2026-11-02 — allow checkout updates on Python-free native hosts)

- Checkout update wrappers use a verified native runtime when Python 3.10 or newer is unavailable, preserve Python selection when both runtimes are installed, and support explicit native selection without fallback.
- Wrapper refusals name the available installation remedies and leave the checkout environment file unchanged.
