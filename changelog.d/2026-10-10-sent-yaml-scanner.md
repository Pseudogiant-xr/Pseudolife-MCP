### Fixed (2026-10-10 — native sent YAML scanner refusals)
- Refuse inter-token/plain-scalar tabs and noncanonical anchor/alias names that PyYAML rejects, while retaining quoted strings, block strings, comments and canonical ASCII anchors.
