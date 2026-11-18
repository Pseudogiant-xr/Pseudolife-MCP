//! The secure random source both binaries draw from (aws-lc, reached through
//! rustls and aws-lc-rs) seeds from the operating system's generator, never
//! from aws-lc's CPU-jitter collector.
//!
//! The jitter collector times its own loops with the raw cycle counter
//! (`rdtsc` on x86-64). Where the cores' counters disagree, as on some
//! virtual machines, a thread moved between cores sees time run backwards;
//! the collector's start-up self-test then fails (`ENOMONOTONIC`) and aws-lc
//! ends the process with `abort()` on its first random draw, with no message
//! (#1688). The repository's `.cargo/config.toml` sets
//! `AWS_LC_SYS_NO_JITTER_ENTROPY=1`, so aws-lc-sys is built without the
//! collector. A build that misses that setting fails this test.

#![allow(unsafe_code)]

// aws-lc prefixes its symbols with its version. An aws-lc-sys upgrade fails
// to link here until the prefix is updated, which is the moment to re-check
// that the new aws-lc still honours the setting.
unsafe extern "C" {
    fn aws_lc_0_45_0_get_entropy_source_method_id_FOR_TESTING() -> i32;
}

/// `OPT_OUT_CPU_JITTER_ENTROPY_SOURCE` in aws-lc's
/// `crypto/fipsmodule/rand/entropy/internal.h`: the operating system seeds
/// the generator. The jitter tree source is `TREE_DRBG_JITTER_ENTROPY_SOURCE`
/// (1).
const OPERATING_SYSTEM_SEEDED: i32 = 2;

#[test]
fn secure_random_is_seeded_by_the_operating_system_not_cpu_jitter() {
    // SAFETY: a C function with no arguments that returns the id of the
    // configured entropy source; it draws nothing and starts no collector.
    let source = unsafe { aws_lc_0_45_0_get_entropy_source_method_id_FOR_TESTING() };
    assert_eq!(
        source, OPERATING_SYSTEM_SEEDED,
        "aws-lc was built with its CPU-jitter entropy source, which aborts the process \
         where cycle counters disagree between cores; build from inside the repository \
         so .cargo/config.toml sets AWS_LC_SYS_NO_JITTER_ENTROPY=1"
    );
}
