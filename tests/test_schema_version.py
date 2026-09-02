"""The single pin on ``SCHEMA_META_VERSION``.

One literal, one test. It exists as a deliberate tripwire: a schema bump
must fail here first, so the author is walked through the rest of the
shipping checklist before the suite goes green again.

There is deliberately no ``>=`` ladder of per-version files behind it. A
``SCHEMA_META_VERSION >= 27`` assertion compares one literal against
another literal in the same repo — it can only fail if someone edits the
constant downward, which is not a failure mode anyone has. The question
those files looked like they answered ("did every version between the
first and the current one really happen?") is answered for real by
``tests/test_release_ux.py`` (the configuration.md history table must
have a row per version, gap-detected, topping out at
``SCHEMA_META_VERSION``) and ``tests/test_atlas_currency.py``
(``docs/atlas/atlas.json`` ``meta.schema`` plus its own migration-list gap
check).
"""

from __future__ import annotations

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

# Bump this literal in the same commit that bumps SCHEMA_META_VERSION.
CURRENT_SCHEMA = 37

_CHECKLIST = """\
SCHEMA_META_VERSION is v{actual}, this pin says v{pinned}.

If you just bumped the schema, this is the tripwire doing its job. Update
CURRENT_SCHEMA above to {actual} and land the rest of the bump in the same
change:

  1. README capabilities table (schema version claim).
  2. docs/guide/configuration.md — the DSN row AND a new row in the
     version-history table (both pinned by tests/test_release_ux.py; the
     history table is gap-detected, so the row is not optional).
  3. CHANGELOG.md — a `v{actual}` mention under [Unreleased]
     (pinned by tests/test_release_ux.py).
  4. docs/atlas/atlas.json — `meta.schema`, plus RE-VERIFY the affected
     storage cards rather than only renumbering
     (pinned by tests/test_atlas_currency.py, which also gap-checks the
     schemaN migration list).
  5. tests/test_migrate_embeddings.py — the two `assert meta[0] == NN`
     literal pins.
  6. `python ops/gen_llms_txt.py` after any README/docs/guide edit
     (tests/test_llms_txt.py pins the generated llms-full.txt).

And test the behaviour the bump added BESIDE ITS CONSUMER — or, if it is
pure DDL shape and nothing reads it yet, add a row to
tests/test_schema_ddl_shape.py. Do not add a new tests/test_schema_vNN.py.
"""


def test_schema_meta_version_is_pinned():
    assert SCHEMA_META_VERSION == CURRENT_SCHEMA, _CHECKLIST.format(
        actual=SCHEMA_META_VERSION, pinned=CURRENT_SCHEMA)
