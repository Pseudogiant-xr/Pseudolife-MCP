//! Reduce the pinned vendor's stderr to a fixed set of descriptions. Its
//! error wrappers can interpolate paths and upstream text, so no source
//! bytes are printed or persisted, including on an unrecognized error.
use std::path::{Path, PathBuf};

use tokio::io::{AsyncRead, AsyncReadExt};

use super::{state, store};

const KNOWN: &[(&[&[u8]], &str)] = &[
    (
        &[b"control plane API key is required"],
        "The vendor did not receive an API key.",
    ),
    (
        &[b"control plane API key is malformed"],
        "The vendor rejected the runtime key format.",
    ),
    (
        &[b"read control-plane api key file"],
        "The vendor could not read the runtime-key source.",
    ),
    (
        &[b"read config file"],
        "The vendor could not read its launch configuration.",
    ),
    (
        &[
            b"controlplane client: unexpected status 401",
            b"controlplane client: unexpected status 403",
            b"controlplane client: unexpected metadata status 401",
            b"controlplane client: unexpected metadata status 403",
        ],
        "The tunnel control plane refused authentication or access.",
    ),
    (
        &[
            b"controlplane client: unexpected status 404",
            b"controlplane client: unexpected metadata status 404",
        ],
        "The tunnel control plane could not find the saved tunnel.",
    ),
    (
        &[
            b"controlplane client: poll transport failed (network_error)",
            b"controlplane client: poll transport failed (timeout)",
            b"controlplane client: fetch tunnel metadata:",
        ],
        "The tunnel client could not reach the control plane.",
    ),
];

pub(super) async fn drain(mut input: impl AsyncRead + Unpin) -> u8 {
    let mut buffer = [0; 1088];
    let mut overlap = 0;
    let mut notes = 0;
    while let Ok(length) = input.read(&mut buffer[overlap..]).await {
        if length == 0 {
            break;
        }
        let end = overlap + length;
        notes |= 1;
        for (index, (needles, _)) in KNOWN.iter().enumerate() {
            if needles.iter().any(|needle| {
                buffer[..end]
                    .windows(needle.len())
                    .any(|match_| match_ == *needle)
            }) {
                notes |= 2 << index;
            }
        }
        overlap = end.min(64);
        buffer.copy_within(end - overlap..end, 0);
        buffer[overlap..].fill(0);
    }
    buffer.fill(0);
    std::hint::black_box(&buffer);
    notes
}

pub(super) fn save(root: &Path, name: &str, notes: u8) -> Option<(PathBuf, Option<&'static str>)> {
    let path = store::checked_path(&root.join(format!("{name}.native-runtime.log"))).ok()?;
    let mut summary = String::new();
    let mut reason = None;
    for (index, (_, message)) in KNOWN.iter().enumerate() {
        if notes & (2 << index) != 0 {
            reason.get_or_insert(*message);
            summary.push_str(message);
            summary.push('\n');
        }
    }
    if summary.is_empty() {
        summary.push_str("The vendor reported a diagnostic outside the recognized categories.\n");
    }
    state::atomic_write(&path, summary.as_bytes(), None).ok()?;
    Some((path, reason))
}
