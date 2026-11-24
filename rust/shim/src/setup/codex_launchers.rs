//! The Codex hook launchers (`run.sh`, `run.ps1`), shared by the installer's
//! Codex hook setup and `update`'s Codex step so `update` need not import the
//! installer.
use sha2::{Digest, Sha256};

/// The launchers, byte for byte what this release writes. Codex approves the
/// command naming them, not their bytes, so changing them needs no new
/// approval; the installer and `update` refuse any bytes that are neither
/// these nor in [`PREVIOUS_LAUNCHERS`] (the current sha256 is pinned in
/// `tests/test_codex_hook_launcher.py`, which checks `ops/setup-codex-hooks.py`
/// writes the same bytes).
pub const RUN_SH: &[u8] = b"#!/usr/bin/env bash
# PseudoLife's Codex hooks: runs the script named by $1 from the bundle that
# ./current names. Codex approves the command naming this file, not the
# scripts it runs, so a new bundle needs no new approval. Written and
# verified byte for byte by the PseudoLife installer.
here=$(dirname \"$0\")
fail() { printf 'PseudoLife hooks: %s; rerun the installer and approve the Codex hooks\\n' \"$1\" >&2; exit 1; }
bundle=$(head -c 64 \"$here/current\" 2>/dev/null | tr -d '\\r\\n ')
case \"$bundle\" in *[!0-9a-f]*|'') fail \"$here/current names no script bundle\" ;; esac
[ \"${#bundle}\" -eq 20 ] || fail \"$here/current names no script bundle\"
case \"${1:-}\" in
    session-start.sh|user-prompt-submit.sh|coordination-start.sh|coordination-prompt.sh|session-end.sh) ;;
    *) fail \"no hook script named ${1:-(none)}\" ;;
esac
script=$1
shift
exec bash \"$here/$bundle/$script\" \"$@\"
";
pub const RUN_PS1: &[u8] = b"# PseudoLife's Codex hooks: runs lifecycle.ps1 from the bundle that
# .\\current names. Codex approves the command naming this file, not the
# scripts it runs, so a new bundle needs no new approval. Written and
# verified byte for byte by the PseudoLife installer.
param([string]$Event)
$bundle = ''
try { $bundle = ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'current'))).Trim() } catch {}
if ($bundle -cnotmatch '^[0-9a-f]{20}$') {
    [Console]::Error.WriteLine(\"PseudoLife hooks: $PSScriptRoot\\current names no script bundle; rerun the installer and approve the Codex hooks\")
    exit 1
}
$global:LASTEXITCODE = 0
& (Join-Path (Join-Path $PSScriptRoot $bundle) 'lifecycle.ps1') -Event $Event
exit $LASTEXITCODE
";
pub const LAUNCHERS: [(&str, &[u8]); 2] = [("run.sh", RUN_SH), ("run.ps1", RUN_PS1)];
/// The sha256 of every launcher an earlier release shipped, by name. The
/// installer and `update` replace one of these with [`LAUNCHERS`] (Codex
/// approves the command naming the file, not its bytes); any other bytes are
/// refused as modified. Changing a launcher? Add the old bytes' sha256 here
/// and in `ops/setup-codex-hooks.py`.
pub const PREVIOUS_LAUNCHERS: [(&str, &[&str]); 2] = [
    (
        "run.sh",
        // Every release from 2026-09-30 until the launcher named a native step.
        &["23dc357bbddc1b4687f9ddfe54f8591b03fbc6e1b19bd41c3aeb2834b0f0a1ce"],
    ),
    (
        "run.ps1",
        &["0ff3ff20c091588aa5b8bf2d2f6859c0ade5a922ba83f9ac8b824415f09d2bc7"],
    ),
];

pub fn launcher_shipped(name: &str, data: &[u8]) -> bool {
    LAUNCHERS
        .iter()
        .any(|(known, bytes)| *known == name && *bytes == data)
        || PREVIOUS_LAUNCHERS.iter().any(|(known, hashes)| {
            *known == name && hashes.contains(&format!("{:x}", Sha256::digest(data)).as_str())
        })
}

/// Whether `data` is a launcher an earlier release shipped under `name`,
/// which a refresh may replace with the current bytes.
pub fn launcher_previous(name: &str, data: &[u8]) -> bool {
    PREVIOUS_LAUNCHERS.iter().any(|(known, hashes)| {
        *known == name && hashes.contains(&format!("{:x}", Sha256::digest(data)).as_str())
    })
}

/// The bytes the releases before this one shipped under `name`, rebuilt from
/// the current ones (a fixture: its test checks them against
/// [`PREVIOUS_LAUNCHERS`]).
#[cfg(test)]
pub(crate) fn earlier_release(name: &str) -> Vec<u8> {
    let (_, current) = LAUNCHERS
        .iter()
        .find(|(known, _)| *known == name)
        .expect("a launcher name");
    String::from_utf8(current.to_vec())
        .unwrap()
        .replace(
            "verified byte for byte by the PseudoLife installer.",
            "verified byte for byte by ops/setup-codex-hooks.py.",
        )
        .replace(
            "rerun the installer and approve the Codex hooks",
            "rerun: python ops/setup-codex-hooks.py",
        )
        .into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hash(data: &[u8]) -> String {
        format!("{:x}", Sha256::digest(data))
    }

    #[test]
    fn the_launchers_name_a_native_repair_and_know_the_earlier_bytes() {
        assert_eq!(
            hash(RUN_SH),
            "24443d25914314c21b81081fa7adbcd0e577860e6a7adb69c9326835998f239b"
        );
        assert_eq!(
            hash(RUN_PS1),
            "2c436e82c4a934bace34d37d73dd0ec9ea398078affaf2151c46b3ac403a0ddb"
        );
        for (name, data) in LAUNCHERS {
            let text = String::from_utf8(data.to_vec()).unwrap();
            assert!(
                text.contains("rerun the installer and approve the Codex hooks"),
                "{name}"
            );
            assert!(!text.contains(".py"), "{name}");
            let earlier = earlier_release(name);
            assert!(launcher_previous(name, &earlier), "{name}");
            assert!(launcher_shipped(name, &earlier), "{name}");
            assert!(!launcher_previous(name, data), "{name}");
        }
        assert_eq!(
            hash(&earlier_release("run.sh")),
            "23dc357bbddc1b4687f9ddfe54f8591b03fbc6e1b19bd41c3aeb2834b0f0a1ce"
        );
        assert_eq!(
            hash(&earlier_release("run.ps1")),
            "0ff3ff20c091588aa5b8bf2d2f6859c0ade5a922ba83f9ac8b824415f09d2bc7"
        );
        assert!(!launcher_shipped("run.sh", b"echo changed\n"));
        assert!(!launcher_shipped("run.ps1", &earlier_release("run.sh")));
    }
}
