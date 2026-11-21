"""Replay the board's captured host changes inside a disposable seed transaction."""
import hashlib


BOOTSTRAP_CODES = ("BOARDTESTA", "BOARDTESTB", "BOARDTESTC")


def issue_bootstrap(rows, ctx):
    """Provide the first host code or one of the two refusal-case replacements."""
    rows.conn.execute("LOCK TABLE maintainer_passkeys IN SHARE ROW EXCLUSIVE MODE")
    if rows.conn.execute("SELECT 1 FROM maintainer_passkeys WHERE state IN ('pending','active')").fetchone():
        raise AssertionError("the bootstrap fixture needs a bank without a live passkey")
    index = ctx.vals.get("bootstrap_issue", 0)
    code = BOOTSTRAP_CODES[index]
    rows.conn.execute("DELETE FROM maintainer_bootstrap WHERE used_at IS NULL")
    rows.insert("maintainer_bootstrap", [rows.row(
        "host_bootstrap", code_hash=hashlib.sha256(code.encode("ascii")).hexdigest(), expires=rows.now + 600)])
    ctx.vals.update(code=code, bootstrap_issue=index + 1)


def confirm_key(rows, credential):
    label = rows.one(
        "UPDATE maintainer_passkeys SET state='active',active_from=%s "
        "WHERE credential_id=%s AND state='pending' RETURNING label",
        (rows.now, credential))["label"]
    rows.event("host_key_change", change="confirm", credential=credential, label=label)


def revoke_key(rows, credential):
    prior = rows.one("SELECT label,state FROM maintainer_passkeys WHERE credential_id=%s FOR UPDATE",
                     (credential,))
    if prior["state"] == "revoked":
        return
    rows.conn.execute(
        "UPDATE maintainer_passkeys SET state='revoked',revoked_at=%s,revoked_by='host' WHERE credential_id=%s",
        (rows.now, credential))
    rows.conn.execute(
        "UPDATE console_sessions SET revoked_at=EXTRACT(EPOCH FROM clock_timestamp())::double precision "
        "WHERE method='passkey' AND issuer=%s AND revoked_at IS NULL", (credential,))
    rows.event("host_key_change", change="revoke", credential=credential, label=prior["label"])


def insert_spare(rows, signer, label):
    rows.insert("maintainer_passkeys", [rows.row(
        "host_spare", credential=signer.id, public_key="\\x" + signer.cose_key().hex(),
        sign_count=signer.sign_count, label=label, active_from=rows.now, state="active")])


def _end_role(rows, name, held, *, expired=False):
    rows.event("operator_end_event", name=name, fence=held["fence"], holder=held["holder_agent_id"],
               event="lease_expire" if expired else "lease_break",
               actor="daemon" if expired else "operator")


def break_coordinator(rows, project):
    name = f"coordinator:{project}"
    held = rows.one("SELECT holder_agent_id,fence FROM coordination_leases WHERE name=%s FOR UPDATE", (name,))
    if held["holder_agent_id"] is None:
        raise AssertionError("the rollback fixture needs a coordinator to break")
    if rows.conn.execute("SELECT 1 FROM coordination_lease_waiters WHERE name=%s", (name,)).fetchone():
        raise AssertionError("the rollback fixture needs the coordinator queue empty")
    rows.conn.execute(
        "UPDATE coordination_leases SET holder_agent_id=NULL,holder_principal='',purpose='',"
        "acquired_at=NULL,expires_at=NULL,expect=NULL,expected_end=NULL,freed_at=%s WHERE name=%s",
        (rows.now, name))
    _end_role(rows, name, held)


def operator_role(rows, project, agent, *, coordinator=False):
    """Place a captured role hold, including the prior holder's expiry event."""
    role = "coordinator" if coordinator else "conductor"
    name = f"{role}:{project}"
    coordinator_name = f"coordinator:{project}"
    legacy = f"delegate:{project}"
    names = [f"conductor:{project}", coordinator_name, legacy]
    present = {r[0] for r in rows.conn.execute(
        "SELECT name FROM coordination_leases WHERE name=ANY(%s) ORDER BY name FOR UPDATE", (names,))}
    person = rows.one(
        "SELECT principal,project,task,credential_hash FROM coordination_agents WHERE agent_id=%s", (agent,))
    if person["credential_hash"] is None:
        raise AssertionError(f"the {name} fixture cannot grant a role to revoked agent {agent}")
    if rows.conn.execute(
            "SELECT 1 FROM coordination_lease_waiters WHERE name=%s AND agent_id=%s",
            (coordinator_name, agent)).fetchone():
        raise AssertionError(f"the {name} fixture cannot grant a role to agent {agent} queued on {coordinator_name}")
    missing = [rows.row("empty_role", name=slot) for slot in names if slot not in present]
    if missing:
        rows.insert("coordination_leases", missing)
    # These scenarios write one role. A preexisting unrelated coordinator
    # on a copy of a bank is outside that write.
    queues = [] if coordinator else [name, legacy]
    if rows.conn.execute(
        "SELECT 1 FROM coordination_lease_waiters WHERE name=ANY(%s) "
        "UNION ALL SELECT 1 FROM coordination_leases WHERE "
        "(name=ANY(%s) AND name<>%s AND holder_agent_id=%s AND expires_at>%s) "
        "OR (name=%s AND NOT %s AND holder_agent_id IS NOT NULL)",
        (queues, names, name, agent, rows.now, legacy, coordinator)).fetchone():
        raise AssertionError(f"the {project} role fixture needs the other role transitions settled")
    held = rows.one("SELECT holder_agent_id,fence,expires_at FROM coordination_leases WHERE name=%s", (name,))
    replaced = held["holder_agent_id"]
    if replaced is not None and held["expires_at"] <= rows.now:
        _end_role(rows, name, held, expired=True)
        replaced = None
    rows.conn.execute("DELETE FROM coordination_leases WHERE name=%s", (name,))
    fence = rows.next("coordination_lease_fence")
    rows.insert("coordination_leases", [rows.row(
        "operator_role", name=name, holder=agent, principal=person["principal"], fence=fence,
        purpose="assigned by the operator" if coordinator else "granted by the operator",
        expires=rows.now + 3600)])
    rows.event("operator_role_event", name=name, holder=agent, fence=fence, replaced=replaced,
               project=person["project"], task=person["task"],
               event="lease_assign" if coordinator else "lease_conductor")
