# Repaired Unix account comparison

The [published instrument](../../rust_port/users_comparison/README.md) executed against committed source `245baf71e697a5b236dbf89928bdd942dafde983` and tree `57a97ffc0d89b2d20ea2a44ddf0738917612bcd9`. [comparison.json](comparison.json) records its exact Git blobs and executed file bytes, commands, exits and path-free observed output. Cargo manifest and lock files have checkout CRLF line endings; their normalized content equals the canonical Git blobs. A subsequent receipt-only commit does not become this execution's source identity.

Actual `users` 0.11.0 and the production replacement matched four name cases, with two positive accounts, across four threads; real UID comparison also passed. All six imported resolver tests passed, including the two defensive admission cases. Four original test bodies remain byte-identical to `43e05ac1`.

The comparison covers this Linux account database only. Android, other Unix targets, unusual NSS backends and different real/effective UIDs remain untested. Error status with a populated result and successful status with a null home are deliberately rejected; the synthetic tests do not establish former-crate equivalence for malformed libc results. The runtime workspace remains free of `users`; only the isolated comparison project depends on it.

The twelve workspace checks recorded historically at `43e05ac1` precede this repair. They remain historical, not refreshed proof. This receipt covers the two commands listed in the JSON; full suites, hosted CI and independent review are outside it.
