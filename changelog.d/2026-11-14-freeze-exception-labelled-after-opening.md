### Fixed (2026-11-14 — a pull request labelled `freeze-exception` after it was opened can join a batch)

- `ops/integrate.py build` and `open` judged a member's CI by every run on its head, so the `Python freeze` run made at the push, before the label, kept the pull request out after the label's own run had passed. The batch tool now judges each check by its latest run on the head, as `merge` and `gh pr checks` do; a newer failure still keeps a head out.
- The `Python freeze` check reads the pull request's labels when it runs instead of from its event, so a rerun of a run made before the label was added passes, and one made before it was removed fails. If the labels cannot be read it judges without them, which refuses only a change that needs the exception; rerun it once the API answers.
