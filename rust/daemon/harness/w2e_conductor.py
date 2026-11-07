"""Conductor reminder admission on the memory-changes hook.

The explicit SQL fixtures represent persisted operator grants, including
malformed and legacy records; no request can mint these privileged rows.
"""
import json

from w2e_hooks import changes


def register(h):
    seed = [h.store("The release checklist has been reviewed.", source="status")]
    ids = {}
    cases = [
        ("current", "conductor:sample", "lease_conductor", "operator", "default", True, 7, 7, 4102444800),
        ("legacy", "delegate:sample", "lease_delegate", "maintainer", "default", True, 8, 8, 4102444800),
        ("foreign", "conductor:foreign", "lease_conductor", "operator", "other", True, 9, 9, 4102444800),
        ("expired", "conductor:expired", "lease_conductor", "operator", "default", True, 10, 10, 1),
        ("wrong-fence", "conductor:fence", "lease_conductor", "operator", "default", True, 11, 12, 4102444800),
        ("wrong-actor", "conductor:actor", "lease_conductor", "agent", "default", True, 13, 13, 4102444800),
        ("no-credential", "conductor:credential", "lease_conductor", "operator", "default", False, 14, 14, 4102444800),
        ("wrong-name", "conductor:name", "lease_conductor", "operator", "default", True, 15, 15, 4102444800),
    ]
    for i, (name, lease, event, actor, principal, credential, fence, recorded, expiry) in enumerate(cases, 1):
        agent = f"{i:032x}"
        ids[name] = agent
        credential_sql = "'fixture-hash'" if credential else "NULL"
        payload = json.dumps({"name": "delegate:name" if name == "wrong-name" else lease,
                              "fence": recorded})
        seed.extend([
            h.sql("INSERT INTO coordination_agents "
                  "(agent_id,principal,credential_hash,created_at,last_activity) "
                  f"VALUES ('{agent}','{principal}',{credential_sql},1700000000,1700000000)", after_stop=True),
            h.sql("INSERT INTO coordination_leases "
                  "(name,holder_agent_id,holder_principal,fence,acquired_at,expires_at) "
                  f"VALUES ('{lease}','{agent}','{principal}',{fence},1700000000,{expiry})", after_stop=True),
            # The next free seq, in case order: on an empty bank that is i;
            # a real bank's copy already holds its own events (mirror/forced.py).
            h.sql("INSERT INTO coordination_events "
                  "(seq,event,actor,agent_id,payload,created_at,prev_hash,hash) "
                  "SELECT COALESCE(MAX(seq), 0) + 1, "
                  f"'{event}','{actor}','{agent}','{payload}',1700000000,'fixture','fixture' "
                  "FROM coordination_events",
                  after_stop=True),
        ])
    ops = [changes(), changes("agent=malformed"), changes("agent=" + "f" * 32)]
    ops += [changes(f"agent={agent}") for agent in ids.values()]
    ops += [changes(f"agent={ids['current']}&since=1"),
            changes(f"agent={ids['current']}&since=@prev"),
            changes(f"agent={ids['current']}", auth=False)]
    return {
        "hook-conductor-reminder": {"config": None, "seed": seed, "ops": ops},
        "hook-conductor-disabled": {
            "config": "coordination:\n  enabled: false\n", "seed": seed,
            "ops": [changes(f"agent={ids['current']}")]},
        "hook-conductor-denied": {
            "config": "coordination:\n  allowed_principals: [other]\n", "seed": seed,
            "ops": [changes(f"agent={ids['current']}")]},
    }
