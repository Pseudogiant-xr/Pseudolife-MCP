//! The Windows protection of a saved tunnel key: user-scoped DPAPI, sealed
//! as `tunnel_profiles._dpapi` seals it (no description, no entropy, no
//! prompt, `CRYPTPROTECT_UI_FORBIDDEN`). Only whether the key unseals to a
//! valid key leaves this module: the plaintext is wiped before it is freed
//! and is never formatted, logged or returned.
use windows_sys::Win32::Foundation::LocalFree;
use windows_sys::Win32::Security::Cryptography::{
    CRYPT_INTEGER_BLOB, CRYPTPROTECT_UI_FORBIDDEN, CryptUnprotectData,
};

/// Whether `sealed` unseals, for this account in this session, to a valid
/// tunnel key: `Some(true)` or `Some(false)` (it unsealed to bytes that are
/// not a key). `None` when it cannot be unsealed here: a blob sealed by
/// another account or another machine, a damaged blob, or a session without
/// the account's logon credentials (a key-authenticated SSH logon), which
/// DPAPI does not tell apart.
#[allow(unsafe_code)]
pub(super) fn holds_key(sealed: &[u8]) -> Option<bool> {
    let size = u32::try_from(sealed.len()).ok()?;
    let input = CRYPT_INTEGER_BLOB {
        cbData: size,
        // CryptUnprotectData only reads its input.
        pbData: sealed.as_ptr().cast_mut(),
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: std::ptr::null_mut(),
    };
    // SAFETY: `input` points at `sealed`, alive for the call; every optional
    // argument is null; on success `output` receives a LocalAlloc buffer of
    // `cbData` bytes, which is read, wiped and freed below and nowhere else.
    let unsealed = unsafe {
        CryptUnprotectData(
            &input,
            std::ptr::null_mut(),
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if unsealed == 0 || output.pbData.is_null() {
        return None;
    }
    // SAFETY: as above, `output` describes a buffer this call owns.
    let plain = unsafe { std::slice::from_raw_parts_mut(output.pbData, output.cbData as usize) };
    let valid = match crate::credentials::decode_token(plain) {
        Ok(token) => {
            wipe(token.into_bytes());
            true
        }
        Err(_) => false,
    };
    plain.fill(0);
    // SAFETY: the buffer was allocated by CryptUnprotectData with LocalAlloc.
    unsafe { LocalFree(output.pbData.cast()) };
    Some(valid)
}

fn wipe(mut bytes: Vec<u8>) {
    bytes.fill(0);
    // Keep the wipe from being dropped as a dead store before the free.
    std::hint::black_box(&bytes);
}

/// Seal `plain` as the Python client's `set_key` does, for tests that need
/// a key file in this platform's saved form. Panics where this session
/// cannot seal (a key-authenticated SSH logon): such a test needs a session
/// signed in with the account's password, which hosted runners have.
#[cfg(test)]
#[allow(unsafe_code)]
pub(super) fn seal(plain: &[u8]) -> Vec<u8> {
    use windows_sys::Win32::Security::Cryptography::CryptProtectData;
    let input = CRYPT_INTEGER_BLOB {
        cbData: u32::try_from(plain.len()).unwrap(),
        pbData: plain.as_ptr().cast_mut(),
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: std::ptr::null_mut(),
    };
    // SAFETY: as in `holds_key`; the output buffer is copied, then freed.
    let sealed = unsafe {
        CryptProtectData(
            &input,
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    assert!(
        sealed != 0,
        "CryptProtectData failed: {} (a key-authenticated SSH session cannot seal; run where the account signed in with its password)",
        std::io::Error::last_os_error()
    );
    // SAFETY: on success `output` is a LocalAlloc buffer of `cbData` bytes.
    let data =
        unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize) }.to_vec();
    unsafe { LocalFree(output.pbData.cast()) };
    data
}
