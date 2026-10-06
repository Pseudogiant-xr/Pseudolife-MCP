use super::*;

#[test]
#[allow(unsafe_code)]
fn test_reentrant_lookup_grows_and_copies_non_utf8_home() {
    #[cfg(not(target_os = "android"))]
    use std::os::unix::ffi::OsStrExt;
    let mut sizes = Vec::new();
    let mut home = b"/fixture/".to_vec();
    home.extend(std::iter::repeat_n(b'x', 8192));
    home.push(0xff);
    home.push(0);
    // SAFETY: the fixture returns only entry itself and a NUL-terminated home
    // in the live provided buffer, or ERANGE before the buffer is large enough.
    let copied = unsafe {
        home_with_lookup("fixture-account", |name, entry, buffer, result| {
            assert!(name.as_bytes() == b"fixture-account");
            sizes.push(buffer.len());
            if buffer.len() < home.len() {
                return libc::ERANGE;
            }
            for (destination, source) in buffer.iter_mut().zip(&home) {
                *destination = *source as libc::c_char;
            }
            entry.pw_dir = buffer.as_mut_ptr();
            *result = entry;
            0
        })
    }
    .unwrap();
    assert_eq!(sizes, [2048, 4096, 8192, 16384]);
    #[cfg(not(target_os = "android"))]
    assert!(copied.as_os_str().as_bytes() == &home[..home.len() - 1]);
    #[cfg(target_os = "android")]
    assert!(copied == PathBuf::from("/var/empty"));
}

#[test]
#[allow(unsafe_code)]
fn test_unknown_accounts_and_lookup_errors_remain_missing() {
    for status in [0, libc::ENOENT, libc::EIO] {
        // SAFETY: null result never causes the zeroed entry to be read.
        let home = unsafe {
            home_with_lookup("fixture-missing", |_, _, _, result| {
                *result = std::ptr::null_mut();
                status
            })
        };
        assert!(home.is_none());
    }
}

#[test]
#[allow(unsafe_code)]
fn test_lookup_rejects_unexpected_result_storage() {
    // SAFETY: all passwd fields admit zero initialization; a different result
    // pointer is rejected without reading either entry or its fields.
    let mut other: libc::passwd = unsafe { std::mem::zeroed() };
    let home = unsafe {
        home_with_lookup("fixture-account", |_, _, _, result| {
            *result = &mut other;
            0
        })
    };
    assert!(home.is_none());
}

#[test]
#[allow(unsafe_code)]
fn test_nul_name_is_rejected_before_account_lookup() {
    // SAFETY: the invalid name is rejected before the lookup is invoked.
    let home = unsafe {
        home_with_lookup("fixture\0account", |_, _, _, _| {
            panic!("NUL name reached account lookup")
        })
    };
    assert!(home.is_none());
    assert!(home_by_name("fixture\0account").is_none());
}
