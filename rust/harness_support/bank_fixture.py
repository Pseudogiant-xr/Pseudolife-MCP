"""Constructor-equivalent bank setup for disposable Rust harness fixtures."""
import time

from .database_guard import assert_disposable_database, fixture_database_pattern
from .schema import ensure_schema


_FIXTURE_DATABASE = fixture_database_pattern()

# Captured from the Python constructor at 7f2e6eacf. The fixture lanes compared
# full database catalogs and rows, including the relation clock and writer epoch.
RELATIONS = (
    ("depends-on", "src requires dst to function", True, None),
    ("part-of", "src is a component of dst", True, None),
    ("hosts", "src is the host/platform for dst", False, None),
    ("runs-on", "src executes on host/platform dst", False, "hosts"),
    ("uses", "src makes use of dst", False, None),
    ("configures", "src sets configuration for dst", False, None),
    ("stores-data-in", "src persists its data in dst", False, None),
    ("implements", "src (a source file) realizes concept/role dst", False, None),
    ("related-to", "untyped catch-all association", False, None),
    ("prefers", "src (a task-type) prefers approach/tool dst (positive lesson)", False, None),
    ("avoids", "src (a task-type) should avoid dead-end dst (negative lesson)", False, None),
)


def require_fixture_database(name):
    """Refuse database names outside the disposable harness slices."""
    if not _FIXTURE_DATABASE.fullmatch(name):
        raise ValueError("bank fixtures require a disposable harness database (pl_cf_<slice>_<name>)")


def assert_fixture_database(conn):
    """Check both production identity and the server's disposable name."""
    require_fixture_database(assert_disposable_database(conn))


def initialize_bank(conn, *, fresh=False):
    """Seed built-in relations and start or advance the writer epoch.

    Graph fixtures require an empty bank and pin the relation clock to 1.0.
    Dream and principal fixtures can open templates with an existing epoch.
    The caller owns the transaction and the slice-specific DSN guard.
    """
    assert_fixture_database(conn)
    ensure_schema(conn)
    relation_insert = ("INSERT INTO public.relations "
                       "(name,description,transitive,inverse_of,builtin,created_at) "
                       "VALUES (%s,%s,%s,%s,TRUE,%s)")
    epoch_insert = "INSERT INTO public.meta (key,value) VALUES ('writer_lease_epoch','1'::jsonb)"
    if not fresh:
        relation_insert += " ON CONFLICT (name) DO NOTHING"
        epoch_insert += (" ON CONFLICT (key) DO UPDATE SET value=to_jsonb(COALESCE("
                         "CASE WHEN jsonb_typeof(meta.value)='number' "
                         "THEN (meta.value #>> '{}')::numeric::bigint END,0)+1)")
    for relation in RELATIONS:
        conn.execute(relation_insert, (*relation, 1.0 if fresh else time.time()))
    conn.execute(epoch_insert)
