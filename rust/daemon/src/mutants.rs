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

/// Let the trust-bind corpus exercise the configured host policy while
/// its actual listener stays on loopback. Release builds never override it.
pub fn loopback_bind_fixture() -> bool {
    supports_loopback_bind_fixture()
        && std::env::var("PSEUDOLIFE_DAEMON_TEST_LOOPBACK_BIND").is_ok_and(|v| v == "1")
}

pub fn supports_loopback_bind_fixture() -> bool {
    cfg!(all(feature = "mutants", debug_assertions))
}

#[cfg(all(test, not(all(feature = "mutants", debug_assertions))))]
mod tests {
    #[test]
    fn loopback_bind_override_is_absent_from_production_build() {
        // CI also runs this with the fixture environment switch set to 1.
        assert!(!super::loopback_bind_fixture());
    }
}
