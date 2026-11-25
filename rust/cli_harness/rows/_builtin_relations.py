"""The built-in relations the frozen storage seeds whenever it opens a bank
(PostgresStorage._seed_relations: INSERT ... ON CONFLICT (name) DO
NOTHING, builtin TRUE). The transfer row's source and target banks hold
them as that storage left them.

Copied verbatim, by syntax tree, from pseudolife_memory/storage/postgres.py at 6ca817d70:
_BUILTIN_RELATIONS :202-222.

The source is frozen, so the copy cannot drift from it; never edit it by hand.
"""
from __future__ import annotations



_BUILTIN_RELATIONS = (
    # (name, description, transitive, inverse_of)
    ("depends-on", "src requires dst to function", True, None),
    ("part-of", "src is a component of dst", True, None),
    ("hosts", "src is the host/platform for dst", False, None),
    ("runs-on", "src executes on host/platform dst", False, "hosts"),
    ("uses", "src makes use of dst", False, None),
    ("configures", "src sets configuration for dst", False, None),
    ("stores-data-in", "src persists its data in dst", False, None),
    # Curation review (2026-07-26) proposes this for a source file paired with
    # its own bare stem, so it has to be seeded: an unregistered suggestion is
    # rejected by `graph_relate` with an empty `suggestions` list.
    ("implements", "src (a source file) realizes concept/role dst", False, None),
    ("related-to", "untyped catch-all association", False, None),
    # Procedural / outcome memory (schema v10): a task-type entity prefers/avoids
    # the tool/source a lesson is about. Untyped like the other builtins.
    ("prefers", "src (a task-type) prefers approach/tool dst (positive lesson)",
     False, None),
    ("avoids", "src (a task-type) should avoid dead-end dst (negative lesson)",
     False, None),
)
