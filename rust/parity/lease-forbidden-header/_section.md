## Lease forbidden-header implementation candidate

The native check/list/run candidate now propagates a fatal forbidden-header
failure before local fallback, lock creation or child launch. Instance reply
values are inspected before retaining a board session; a refused registration
therefore sends no lease or release. Environment bearers are inspected before
credential decoding, and file bearers after the existing private-file checks.
The ordinary decoder's trailing CR/LF file terminator remains outside the
header value. Checked header constructors retain ordinary non-ASCII/surrogate
failures and unrelated fallback behavior.

`evals.rust_port.lease_headers` adds executable inputs for the retained eight
instance cases, bearer/folded/all-C0 cases and field/output/file/request mutation
controls. It preserves raw pinned Python captures and separately labels the
approved refusal expectation. Historical preparation rows above retain their
old dispositions; they are not final-head acceptance receipts. CLI-LEASE
remains deferred, including successful-child invocation windows, Windows
unlock/Ctrl-C, hosted acceptance and measurements.
