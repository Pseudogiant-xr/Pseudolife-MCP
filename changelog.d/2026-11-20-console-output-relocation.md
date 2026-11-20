### Changed (2026-11-20 — Keep the Console independent of the Python daemon)
- The daemon images use the committed Console build from `frontend/dist/`. Both images and the Python wheel continue to ship the Console at their existing runtime paths.
- For contributors running the frozen Python daemon from a checkout or an ordinary editable install, `/ui/` now returns 404. Use the Console's Vite dev server during development.
