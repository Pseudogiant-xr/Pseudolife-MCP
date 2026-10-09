# Shipped-producer census

Port slices need the inputs shipped clients send. `producer-census.json` indexes
their source locations and input shapes against `contract-inventory.json`; it
does not promote a PARITY row or authorize a defer decision.

Run from the repository root:

```sh
python rust/producer_census.py
python rust/producer_census.py --check
python rust/producer_census.py --ci-check
python -m pytest rust/test_contract_inventory.py rust/test_producer_census.py -q
```

The generator uses only the standard library, source text, Python ASTs and Git;
it never imports or contacts the daemon. The inventory's `oracle_commit` stays
historical. `input_sha256` identifies the exact scanned tracked working-tree text
(UTF-8, BOM removed, CRLF normalized to LF), independently of commit history and
squash merges. Modified tracked files and newly staged producers are included;
untracked and ignored scratch files are excluded using `git ls-files`. A changed
tracked file, contract or extraction result makes the currency test fail.

Parser tests gate every Rust CI run. Currency also gates when the census script,
generated JSON or contract inventory changes; other source drift is checked and
reported without blocking unrelated port PRs. `--ci-check` selects that policy
from the GitHub event's changed-file range. `--check` always fails on stale data.
Regenerate before consuming the census in a port slice.

`surface` has one entry for every inventory item, plus individual config fields.
IDs are `tool:memory_search`, `route:GET /api/search`, `cli:backup`,
`coordination:receive`, `environment:PSEUDOLIFE_MCP_CONFIG`, or
`config:DreamConfig.max_batch`. Each producer names `location` as `path:line`.
`shape.parameters` maps sent names to a `literal` or a source `expression`;
false, null and empty values remain distinct. CLI records preserve `argv_source`
or AST `argv`, because shell command construction is not executed or emulated.
Both executable and `python -m pseudolife_memory.cli` forms are scanned, including
literal Dockerfile/Rust argv arrays and unresolved forwarded modes.
Config section records collect field evidence, not a complete config document.

`evidence` distinguishes `static`, `documented`, `description-example`, `manual`
and `unverified`. Documentation includes historical plans/specs: verify their
status before using them as current inputs. Environment and dotted config
`usage: reference` records are conservative leads, not proof of an assignment.
Unsupported request syntax remains `dynamic: unresolved`; an unknown verb has
`method: null`, including when associated with both inventory verbs for a path.
Plugin instructions naming a tool without arguments remain unverified
`instruction-reference` records. Coordination calls through MCP tools have
`forwarded_from_tool`, `tool_shape` and `mapping_source`; their wire body stays
unresolved until normalization/default filtering is verified.
Generated Console JS is scanned directly, including its shared get/post helpers
and review action plans. Third-party vendor JS and inline Rust test modules are
excluded. The full input list is recorded in the JSON.

`complete` means the parameter-name set is known, including expression-valued
parameters. `omitted_optional` lists advertised optional names outside that set;
it is **null** if the shape or parameter contract is unknown. It never means
the consumer sent defaults. `conditions` on manual records explains fields
omitted on particular branches or compatibility retries. Query helpers can also
drop null/undefined/empty values at runtime; their source expressions preserve
those conditions. Optional tool names come from current MCP signatures, route
names from current route/query/body reads, and coordination names from its
current parameter table; `parameter_source` records that source separately from
the historical surface inventory.

`inventory_gaps` contains producer targets outside the inventory; the generator
does not add them to it. Some are obsolete documented calls, others newer
shipped targets such as maintainer routes or CLI modes. `no_observed_producer`
lists **provisional** defer candidates. Absence is not proof: inspect
`unresolved`, which records built URLs, forwarding and shell-built commands,
as well as incomplete matched producers. `resolved_by` links reviewed manual
resolutions while retaining the original dynamic location.

For a slice, locate its surface IDs, follow the producer locations at the
recorded source hashes, and turn the confirmed shapes into cases in
the slice's harness. Resolve relevant dynamic records before deferring anything.
Declare any remaining unknown explicitly in the slice spec. The census gives
source evidence; the slice's differential harness proves implementation parity.

Important hook queries/bodies, the Rust episode URL dispatch, heartbeat body
and long-poll receive body have manual resolutions in `producer_census.py`.
Whole-file `MANUAL_SHA256` fingerprints withdraw trusted manual shapes when those
sources change. `manual_drift` reports the affected files and prior/actual hashes;
regeneration continues with unresolved static evidence. Review construction and
conditions, update the resolution/fingerprint, then regenerate before treating
those shapes as canonical. Do not refresh a fingerprint without that review.
Spot-check ten randomly selected records against their cited source before
shipping, and put the seed, locations and result in the PR body.
