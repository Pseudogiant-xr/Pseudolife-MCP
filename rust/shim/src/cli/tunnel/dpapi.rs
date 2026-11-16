//! The Windows protection of a saved tunnel key: user-scoped DPAPI, sealed
//! as `tunnel_profiles._dpapi` seals it (no description, no entropy, no
//! prompt, `CRYPTPROTECT_UI_FORBIDDEN`). Only whether the key unseals to a
//! valid key leaves this module: the plaintext is wiped before it is freed
//! and is never formatted, logged or returned.
use windows_sys::Win32::Foundation::{
    ERROR_INVALID_DATA, ERROR_INVALID_PARAMETER, GetLastError, LocalFree,
};
use windows_sys::Win32::Security::Cryptography::{
    CRYPT_INTEGER_BLOB, CRYPTPROTECT_UI_FORBIDDEN, CryptUnprotectData,
};

/// Whether `sealed` unseals, for this account in this session, to a valid
/// tunnel key. `Some(false)` when it is no usable key: it unseals to bytes
/// that are not a key, or the blob itself is damaged (CryptUnprotectData's
/// `ERROR_INVALID_DATA` for altered or truncated ciphertext,
/// `ERROR_INVALID_PARAMETER` for bytes that are no DPAPI blob; measured on
/// the Windows dev VM, 16 November 2026). `None` for any other failure,
/// where the blob may be sound but cannot be unsealed here: another
/// account's or machine's key (`NTE_BAD_KEY_STATE`, measured the same day),
/// or a logon without the account's credentials, such as a key-based SSH
/// logon.
#[allow(unsafe_code)]
pub(super) fn holds_key(sealed: &[u8]) -> Option<bool> {
    let Ok(size) = u32::try_from(sealed.len()) else {
        return Some(false);
    };
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
    if unsealed == 0 {
        // SAFETY: reads this thread's last-error value, set by the call above.
        let error = unsafe { GetLastError() };
        return matches!(error, ERROR_INVALID_DATA | ERROR_INVALID_PARAMETER).then_some(false);
    }
    if output.pbData.is_null() {
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
    // SAFETY: the buffer was allocated by CryptUnprotectData with LocalAlloc
    // and is not used after this.
    unsafe { LocalFree(output.pbData.cast()) };
    Some(valid)
}

fn wipe(mut bytes: Vec<u8>) {
    bytes.fill(0);
    // Keep the wipe from being dropped as a dead store before the free.
    std::hint::black_box(&bytes);
}

/// Seal `plain` as the Python client's `set_key` does, for tests that need
/// a key file in this platform's saved form. `None`, after saying why, only
/// in an SSH logon that cannot seal (key-based SSH carries no credentials
/// for the account's DPAPI keys, so CryptProtectData is denied there); any
/// other failure panics. Hosted runners and a logon with the account's
/// credentials always run the test.
#[cfg(test)]
#[allow(unsafe_code)]
pub(super) fn seal(plain: &[u8]) -> Option<Vec<u8>> {
    use windows_sys::Win32::Foundation::ERROR_ACCESS_DENIED;
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
    if sealed == 0 {
        // SAFETY: reads this thread's last-error value, set by the call above.
        let error = unsafe { GetLastError() };
        let ssh = std::env::var_os("SSH_CONNECTION").is_some();
        if error == ERROR_ACCESS_DENIED && ssh {
            eprintln!(
                "skipped: CryptProtectData is denied in this SSH logon, which holds no credentials for the account's DPAPI keys; run this test in a logon signed in at the machine (hosted runners are)"
            );
            return None;
        }
        panic!("CryptProtectData failed with Windows error {error}");
    }
    // SAFETY: on success `output` is a LocalAlloc buffer of `cbData` bytes.
    let data =
        unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize) }.to_vec();
    // SAFETY: allocated by CryptProtectData with LocalAlloc, not used after.
    unsafe { LocalFree(output.pbData.cast()) };
    Some(data)
}
