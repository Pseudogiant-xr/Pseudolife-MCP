# Recovering agent mailboxes after a database restore

A database backup contains agent addresses, credential hashes and pending mail.
Restoring it can also restore old attachment and wake permissions. The offline
recovery command revokes those permissions before agents reconnect, while
retaining mailbox contents for deliberate recovery.

Do not run this procedure for an ordinary daemon restart. A restart preserves
credentials and pending mail; an expired attachment can reconnect with a fresh
lease. A raw database restore cannot be detected automatically.

## Restore procedure

1. Take a backup before replacing the database. Stop the daemon and all attached
   adapters. Keep them stopped through recovery; editing configuration cannot
   disable a daemon that already loaded its configuration.
2. Set `coordination.enabled: false` in the configuration the restored daemon
   will use. Retain the explicit `allowed_principals` list needed for later
   mailbox ownership checks.
3. Restore the database using the normal backup procedure. Set
   `PSEUDOLIFE_MCP_DATABASE_URL` in the operator's environment to the restored
   database. Recovery uses this environment variable only: it does not start
   PostgreSQL, launch a daemon, load a model or apply schema migrations.
4. Revoke every restored instance credential and clear attachment/wake state:

   ```console
   pseudolife-mcp coordination-recovery recover --config <config.yaml> --confirm-daemon-stopped --confirm-restore
   ```

   This preserves messages and addresses, advances attachment generations and
   disables wake. Old adapters can no longer authenticate. The command refuses
   an enabled configuration or a missing confirmation. The stopped-daemon flag
   is an operator assertion; it does not inspect or stop running processes.
5. Rebind only the addresses you intend to resume. Each address must retain its
   original principal, which must appear in `coordination.allowed_principals`:

   ```console
   pseudolife-mcp coordination-recovery rebind --config <config.yaml> --confirm-daemon-stopped --agent <agent-id> --principal <principal> --bank-url http://127.0.0.1:8099 --state <new-private-state.json>
   ```

   Use an existing private directory outside the repository and a **new** file
   path for each adapter. Existing files, symlinks and hardlinks are refused.
   The command writes the bank URL, address and fresh credential with owner-only
   file access. It prints no credentials and never overwrites old adapter state.
6. After the intended addresses are rebound, enable coordination in the daemon
   configuration and restart the daemon. Point each adapter at its new explicit
   state file. Wake remains off until separately requested at adapter launch;
   restoring a backup never grants live delivery by itself.

The Python module is also callable directly with the same arguments:
`python -m pseudolife_memory.coordination_recovery`.

## Failure and retention

A failed private-state write rolls back credential issuance. A crash or uncertain
database commit can leave an empty reservation or a state file whose credential
was not committed. Keep coordination disabled, retain the file for diagnosis,
and do not infer success from its existence. The command does not automatically
register a new address or silently replace the file. If the commit outcome cannot
be established, run the restore recovery operation again while offline and
rebind the intended mailboxes into fresh paths; this revokes any previously
rebound credentials too, so update every affected adapter deliberately.

Body expiry still applies to restored mail: receiving never returns expired or
acknowledged messages. Message bodies expire after 24 hours, while request keys
and terminal metadata remain for seven days from creation. Maintenance purges
expired bodies and old metadata during coordination activity. Pending capacity
errors never silently discard mail. The mailbox clock high-water mark survives
message pruning, so restart with a backward wall clock cannot regress its stamps.

Portable knowledge exports exclude agent mailboxes, credentials and operational
metadata. Full database backups retain them. See
[configuration](configuration.md#experimental-agent-coordination) for the
default-off feature and [the experimental design](../specs/2026-09-11-agent-coordination-design.md)
for delivery and acknowledgment semantics.
