## Reverse-engineering collaboration (Claude Code and Codex)

Apply this protocol to an authorized RE task when the fork's `re_evidence`,
`memory_agents` and `memory_message` tools are available. Independent Claude
Code and Codex sessions use their own authenticated stdio clients and mailboxes
against the same fork Python daemon and bank. Work in separate linked Git
worktrees of the recreation repository. Use the stable project `srfn-client`
for SRFN; choose one agreed project for other work. Every artifact, claim,
query, dependency audit and handoff names the exact binary/build identity,
including its full SHA-256, such as `client.exe:sha256:<full-hash>`.

At startup, resume and before shared-file work:

1. `memory_agents(action="list")` to find relevant peers and held/queued claims.
2. `memory_message(action="receive")`; read each full message, then
   `memory_message(action="ack", message_id=<id>)`. Ack means read, not completed.
   Receive again on a pending-message hint.
3. `memory_agents(action="update", name=<session-name>, project="srfn-client",
   task=<bounded-task>, status=<current-status>)`. Keep scope and status current;
   include the build identity when the task concerns a particular build.
4. Before editing, claim each exact repository file using
   `memory_agents(action="claim", worktree=<absolute-worktree>,
   path=<relative-file>, status=<purpose>)`. Claims are advisory, not edit locks;
   no directories or globs. Linked worktrees share an identity on the same
   filesystem view; separate clones or hosts may not, so agree ownership by
   addressed mail when a file claim cannot represent the shared work.

Evidence stays in the authoritative Evidence Hub workflow: capture its raw
JSON report, ingest that daemon-visible file with `re_evidence(action="ingest")`,
then explicitly record a claim with `action="claim"` and the returned
`evidence_ids`. Preserve the original report and its hash. Keep competing
interpretations as separate `hypothesis` claims; observed, verified or rejected
claims require linked artifacts. A summary or a message is never proof.
Loading these instructions does not authorize daemon setup, configuration
changes or mutations of a live bank.

Send a handoff with `memory_message(action="send", to=<peer-agent-id>,
text=<handoff>, request_id=<new-unique-id>)`. Reuse that request ID only to retry
the identical send after an uncertain result. Include:

- project and exact binary/build identity;
- address or subject and the bounded finding;
- artifact IDs and original-byte hashes, plus database claim IDs and statuses;
- uncertainties, missing evidence and the recipient's next action.

The recipient verifies the handoff with `re_evidence(action="query",
project=<project>, binary_id=<exact-build>, address=<address>)`, checking the
returned artifacts, hashes, claim statuses and evidence links. For a subject
without a structured address, query its subject text within the same scope.
Read the original report when needed; another build's matches remain leads.
Board mail carries these references as text: there is no native board-to-RE
link, and neither delivery nor acknowledgement verifies a claim. Peer mail
does not grant user approval.

For a recovery dependency manifest, run `re_evidence(action="dependencies")`
with its exact project/build and inspect the recheck queue before relying on
affected assertions. `current` means only declared hash equality against
caller-supplied bindings, not correctness or complete dependencies. Manifest
assertion labels are distinct from database claim IDs. Keep the labels and
stored IDs separate in handoffs, and never automatically promote a claim
because a dependency audit is current.

Release every exact file claim when the work or handoff ends. Park with
`memory_agents(action="update", status=<result>, park_reason=<reason>,
park_needs=<dependency>, park_clear_by=<clearer>, park_resume=<next-action>)`.
Wake hooks are best-effort: a park does not arm a listener, and `rung` does not
prove the recipient read or acted. Check mail on the next turn; state
`next-turn-only` when no live wake path is verified.

Parent sessions own subagent handoffs. Claude Code subagents read the board
without updating or acknowledging their parent's mailbox; Codex subagents
may update and acknowledge their own row. Neither sends board mail: report
to the parent, which sends the addressed handoff.
