| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| MAIL-RING-V53 | 1/2/4 | wait-mail wakes on an authorized daemon ring, plain addressed mail never wakes; digest/watermark cleanup, mailbox attribution to parent/subagent, generation/attachment continuity and optional Codex mailbox-tool approval. `wait_mail_cli.py`, `coordination_adapter.py`, `ops/setup-codex-hooks.py` | `test_wait_mail_cli.py`, `test_coordination_adapter.py`, `test_codex_subagent_stop.py`, `test_codex_hook_setup.py` I/mixed | deferred |
