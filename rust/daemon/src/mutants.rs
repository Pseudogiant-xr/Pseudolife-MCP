//! Deliberate breaks for the harness's mutant control (`harness/run.py
//! mutants`). Compiled in only with `--features mutants`; a normal build has
//! no mutant code path and `active` is always false.

/// True when this build carries mutants and `PSEUDOLIFE_DAEMON_MUTANT`
/// names `name`.
#[cfg(feature = "mutants")]
pub fn active(name: &str) -> bool {
    std::env::var("PSEUDOLIFE_DAEMON_MUTANT").is_ok_and(|v| v == name)
}

#[cfg(not(feature = "mutants"))]
#[inline(always)]
pub fn active(_name: &str) -> bool {
    false
}
