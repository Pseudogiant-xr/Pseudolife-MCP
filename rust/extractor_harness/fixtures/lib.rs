//! Target-native fixtures travel inside nextest archives; runners need no compiler.
pub const STUB: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/stub.bin"));
pub const LAUNCHER: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/launcher.bin"));
