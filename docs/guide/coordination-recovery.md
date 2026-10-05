# Recovering agent mailboxes after a database restore

A database backup contains agent addresses, credential hashes and pending mail.
Restoring it can also restore old attachment and wake permissions. The offline
recovery command revokes those permissions before agents reconnect, while
retaining mailbox contents for deliberate recovery.

Do not run this procedure for an ordinary daemon restart. A restart preserves
credentials and pending mail; an expired attachment can reconnect with a fresh
lease. A raw database restore cannot be detected automatically.

## Restore procedure

The authenticated bank identity is stored in the existing `meta` table and is
preserved by database backup and restore. A restored clone therefore represents
the same logical bank; it is not automatically a new independent mailbox
authority. The restore procedure below still revokes restored instance
credentials and requires deliberate rebinds.

1. Take a backup before replacing the database. Stop the daemon and all attached
   adapters. Keep them stopped through recovery; editing configuration cannot
   disable a daemon that already loaded its configuration.
2. Set `coordination.enabled: false` in the configuration the restored daemon
   will use; coordination is on by default, so a configuration without the key
   is refused. Retain the `allowed_principals` list needed for later mailbox
   ownership checks (without the key, only `default` is allowed).
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
   pseudolife-mcp coordination-recovery rebind --config <config.yaml> --confirm-daemon-stopped --agent <agent-id, or a unique prefix of 8 or more characters> --principal <principal> --bank-url http://127.0.0.1:8765 --state <new-private-state.json>
   ```

   Use an existing private directory outside any Git repository and a **new**
   file path for each adapter. Existing files, symlinks and hardlinks are
   refused, and so is a state path under any directory that contains a
   `.git` entry (the command walks every parent), so a checkout or a worktree
   cannot end up holding a mailbox credential. `--bank-url` is the daemon's
   own address (the compose stack serves `8765`).
   The command writes the bank URL, address and fresh credential with owner-only
   file access. It prints no credentials and never overwrites old adapter state.
6. After the intended addresses are rebound, enable coordination in the daemon
   configuration and restart the daemon. Point each adapter at its new explicit
   state file. Wake remains off until separately requested at adapter launch;
   restoring a backup never grants live delivery by itself.

The Python module is also callable directly with the same arguments:
`python -m pseudolife_memory.coordination_recovery`.

## A move is not a restore

`pseudolife-mcp move` ([moving a bank](remote-bank.md#moving-a-bank))
restores a backup onto another host but does not run this procedure. The
procedure revokes restored credentials because a restored bank could be a
second copy of a live one. A move is not a copy: it stops the source daemon
before the final dump and fences the source database (`ALTER DATABASE <db>
WITH ALLOW_CONNECTIONS false`) right after it, so only one live authority
ever serves the bank's mailboxes, and the revocation would protect against
nothing.

The exemption has a limit: it keeps the mailboxes, not the sessions'
addresses. A shim discards its saved adapter state when the bank URL
changes, so every re-pointed session starts with a new address. Mail sent to
an old address stays in the bank, undelivered. The move's report counts it
per old address; to give an address back, follow `rebind` (step 5 of the
restore procedure above). If you ever bring a moved bank's old host back alongside the
new one, it is a second copy, and this procedure applies to it in full.

## The audit log across a restore

The board's [audit log](configuration.md#audit-log) (`coordination_events`,
schema v42) is restored with the database, as of the backup, so everything the
board did after the backup is gone from it too. The restored chain still
verifies, because the chain alone cannot see that its newest rows are missing.
If you recorded a head earlier, `pseudolife-mcp board-audit verify --expect-head
SEQ:HASH` shows whether that head survived. `recover` and each `rebind` append
operator events (`actor: operator`) to the restored log in the same transaction
as the change. A body redacted after the backup was taken is back, and its
`redact` row is gone: run `pseudolife-mcp board-audit redact` for it again
([redacting a body](configuration.md#redacting-a-body)).

A backup taken before v42 restores without the log. Recovery never migrates a
schema, so both commands still revoke and rebind, print that the operation is
not recorded, and the next daemon start creates an empty log.

## Maintainer passkeys across a restore

A full backup holds the maintainer's passkeys (`maintainer_passkeys`, schema
v54), unredeemed enrolment codes (`maintainer_bootstrap`), spent challenge
nonces (`maintainer_nonces`) and the challenge secret (`maintainer_secret_v1`
in `meta`), all as of the backup. `recover` does not touch them. A passkey
revoked after the backup is active again, and one enrolled after it is gone.
Before step 6, with `PSEUDOLIFE_MCP_DATABASE_URL` still pointing at the
restored database, run `pseudolife-mcp maintainer list` and
`pseudolife-mcp maintainer revoke <prefix>` any key you revoked since the
backup. If you cannot tell, `pseudolife-mcp maintainer reset` revokes every
key, rotates the secret and reopens enrolment; enrol again with
`pseudolife-mcp maintainer setup`.

Portable exports never carry these tables or the secret (full backups only),
so an imported bank starts with no passkeys.

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
expired bodies and old metadata during coordination activity. The audit log
keeps its own copy of each body for `coordination.audit_retention_days`
(default 90 days), and restoring a backup restores that copy too. The operator
can remove a body sent from schema v46 on with `board-audit redact`; one sent
before v46 is part of the hashed chain and stays until retention removes it
(`redact` still blanks its live mailbox copy while one is left).
Pending capacity
errors never silently discard mail. The mailbox clock high-water mark survives
message pruning, so restart with a backward wall clock cannot regress its stamps.

### Malformed coordination clock row

Calls that initialize the bank fail with `invalid coordination clock high-water
mark`, reads included, while `/health` can still report the daemon up. The
`coordination_hlc_highwater` value in `meta` must be a two-element list of
non-negative integers: the physical and logical parts of the HLC.

Do not delete this row or reset it to zero. It can be the only surviving bound
for mailbox-only writes after message pruning. Neither the remaining messages
nor the cortex, world and lesson records necessarily contain the latest stamp;
an older backup alone does not cover writes made after that backup.

1. Stop the daemon and adapters, take a database backup, and preserve the damaged
   row for diagnosis before editing it.
2. Establish a verified HLC bound that is at least as high as every stamp issued
   before the corruption, including pruned messages. Use a trustworthy copy of
   the latest high-water mark or complete evidence of subsequent writes. Compare
   stamps as integer pairs, not strings. If no such bound can be established,
   keep the bank stopped; do not guess a replacement or discard the safeguard.
3. In a database session with errors configured to stop execution, replace the
   placeholders below with that verified pair and update only the damaged row:

   ```sql
   BEGIN;
   UPDATE meta SET value = '[<verified-physical>, <verified-logical>]'::jsonb
   WHERE key = 'coordination_hlc_highwater';
   SELECT value FROM meta WHERE key = 'coordination_hlc_highwater';
   COMMIT;
   ```

   Confirm one row was updated and the returned value matches the verified pair.
4. Restart the daemon with adapters still stopped. Re-seeding observes this bound
   alongside the slot stores, so subsequent stamps must exceed it even if the
   wall clock moved backward. Verify initialization succeeds before restarting
   the adapters. Retain the backup and repair evidence.

Portable knowledge exports exclude agent mailboxes, credentials, the audit log,
bank identity and operational metadata. Import also ignores any bank identity in an archive,
preserving the destination's identity. Full database backups retain it. See
[configuration](configuration.md#experimental-agent-coordination) for the
feature's defaults and [the experimental design](../specs/2026-09-11-agent-coordination-design.md)
for delivery and acknowledgment semantics.
