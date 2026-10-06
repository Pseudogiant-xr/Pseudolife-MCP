# Actual which 8.0.6 comparison

These CPU receipts compare the removed registry crate with the production
resolver included by the [standalone instrument](../../rust_port/which_comparison/README.md).
Execution source is `9dddf6fcc37fa329d7b2a5125b3725c09118b32e`, including the
corrected users base `e7dc5a405e6920c6429025c8a8f6d4af7c7a2367`. The later
documentation/receipt carrier does not replace that execution identity.

| Platform | Equal public-API cases | Expected owned-config differences | Unavailable cases |
| --- | ---: | ---: | ---: |
| [Windows](windows.json) | 23 | 1 | 1 |
| [Linux](linux.json) | 17 | 0 | 0 |

Both build and comparison exits are zero. All expected comparisons passed;
owned fixture cleanup was checked. The Windows unavailable case is file-symlink
creation error 1314. Ordinary and dangling directory-junction comparisons passed.
Windows PATHEXT rereading matches actual public `which::which`; it differs from
the actual owned `WhichConfig<RealSys>` cache, as explicitly tested.

The [manifest](manifest.json) records exact source and receipt identities and
focused command exits. Five captured source files remain byte-identical across
platforms and at packaging; canonical Git identities remain separate from
captured checkout-byte hashes. Registry archive checksum and linked crate file
hashes are retained in each receipt. The standalone linked test and clippy pass
on both platforms; workspace resolver tests pass 10/10 on Windows and 9/9 on
Linux with default features and without default features. Formatting also passes.

These are bounded path/acceptance comparisons, not an exhaustive equivalence
claim for ACLs, error types/text, all platforms or concurrent environment mutation.
The standalone instrument is manually invoked and is outside the existing
workspace CI routing. No full-suite or current hosted-matrix result is claimed.
Earlier unsuccessful fixture construction and offline metadata attempts remain
in the private command evidence; they are not successful comparison receipts.
