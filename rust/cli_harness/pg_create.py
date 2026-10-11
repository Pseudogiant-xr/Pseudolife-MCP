"""Copy the shared extension template without disturbing its sessions."""

import time


def create_from_default_template(conn, statement) -> None:
    import psycopg  # noqa: PLC0415

    # The restricted test login inherits untrusted vector from template1;
    # template0 cannot supply it. A concurrent test-login bootstrap briefly
    # connects there, so wait for that session rather than terminating it.
    for attempt in range(5):
        try:
            conn.execute(statement)
            return
        except psycopg.errors.ObjectInUse:
            if attempt == 4:
                raise
            time.sleep(1)
