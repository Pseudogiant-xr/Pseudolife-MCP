#![deny(unsafe_code)]
// Compile the same private proof leaf into a selectable integration target so
// hosted lifecycle stress can exercise its cancellation and cleanup regressions.
use pseudolife_stdio::{board, credentials, lifecycle, pg, stderrln};

#[allow(dead_code)]
#[path = "../src/cli/doctor/proof.rs"]
mod proof;
