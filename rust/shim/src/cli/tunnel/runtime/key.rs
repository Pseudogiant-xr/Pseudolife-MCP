//! The vendor consumes a file reference at startup. On Windows serve it
//! through a local owner-only pipe so the saved DPAPI blob is the only
//! persistent copy of the key.
use std::path::Path;
use uuid::Uuid;

use super::super::{Fail, store};

pub(super) struct Key(Vec<u8>);

impl Drop for Key {
    fn drop(&mut self) {
        self.0.fill(0);
        std::hint::black_box(&self.0);
    }
}

impl Key {
    pub(super) fn matches(&self, other: &[u8]) -> bool {
        self.0 == other
    }

    #[cfg(test)]
    pub(super) fn fixture() -> Self {
        Self(b"fixture-runtime-key".to_vec())
    }

    pub(super) fn load(root: &Path, name: &str) -> Result<Self, Fail> {
        let path = store::key_path(root, name);
        let invalid = || {
            Fail::Detail(format!(
                "cannot read a private runtime key at {}. Check the key and the account running the tunnel",
                path.display()
            ))
        };
        if !store::occupied(&path).map_err(|_| invalid())? {
            return Err(Fail::Detail(format!(
                "profile {name} has no runtime key. Import its private key with tunnel setup before running it"
            )));
        }
        let data = Self(store::private_read(&path, 16384).map_err(|_| invalid())?);
        #[cfg(windows)]
        {
            let sealed = data.0.strip_prefix(b"DPAPI\0").ok_or_else(invalid)?;
            super::super::dpapi::with_key(sealed, |plain| Self(plain.to_vec())).map_err(|error| {
                match error {
                    super::super::dpapi::KeyError::Invalid => invalid(),
                    super::super::dpapi::KeyError::Unavailable => Fail::Tunnel(
                        "Windows cannot unlock the saved runtime key. Run the tunnel in a sign-in under the account that saved it",
                    ),
                }
            })
        }
        #[cfg(unix)]
        {
            let plain = data.0.strip_prefix(b"PLAIN\0").ok_or_else(invalid)?;
            crate::credentials::decode_token(plain)
                .map(|token| Self(token.into_bytes()))
                .map_err(|_| invalid())
        }
    }

    #[cfg(unix)]
    pub(super) fn stage(self, directory: &Path, _launch: Uuid) -> Result<Staged, Fail> {
        let path = directory.join("key");
        crate::board::state::atomic_create(&path, &self.0).map_err(|_| super::UNSAFE_STORE)?;
        Ok(Staged {
            reference: format!("file:{}", path.display()),
        })
    }

    #[cfg(windows)]
    pub(super) fn stage(self, _directory: &Path, launch: Uuid) -> Result<Staged, Fail> {
        let pipe_name = format!(r"\\.\pipe\pseudolife-tunnel-{}", launch.simple());
        let server = pipe(&pipe_name).map_err(|_| Fail::Tunnel(
            "cannot create the local private runtime-key pipe. Check the account's access and retry",
        ))?;
        let task = tokio::spawn(deliver(server, self));
        Ok(Staged {
            reference: format!("file:{pipe_name}"),
            _task: tokio_util::task::AbortOnDropHandle::new(task),
        })
    }
}

pub(super) struct Staged {
    pub(super) reference: String,
    #[cfg(windows)]
    _task: tokio_util::task::AbortOnDropHandle<std::io::Result<()>>,
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn pipe(name: &str) -> std::io::Result<tokio::net::windows::named_pipe::NamedPipeServer> {
    use crate::credentials::windows_security::current_user_sid;
    use std::{ffi::c_void, io, mem::size_of, ptr};
    use tokio::net::windows::named_pipe::ServerOptions;
    use windows_sys::Win32::{
        Foundation::LocalFree,
        Security::{
            Authorization::ConvertStringSecurityDescriptorToSecurityDescriptorW,
            SECURITY_ATTRIBUTES,
        },
    };

    struct Descriptor(*mut c_void);
    impl Drop for Descriptor {
        fn drop(&mut self) {
            // SAFETY: descriptor is a unique LocalAlloc result.
            unsafe { LocalFree(self.0) };
        }
    }
    let sid = current_user_sid().ok_or_else(|| io::Error::other("no Windows account SID"))?;
    let sddl: Vec<u16> = format!("D:P(A;;GA;;;{sid})")
        .encode_utf16()
        .chain([0])
        .collect();
    let mut descriptor = ptr::null_mut();
    // SAFETY: the descriptor out pointer and NUL-terminated SDDL are live.
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            1,
            &mut descriptor,
            ptr::null_mut(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    let descriptor = Descriptor(descriptor);
    let mut attributes = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.0,
        bInheritHandle: 0,
    };
    // SAFETY: the initialized descriptor and attributes outlive pipe creation.
    unsafe {
        ServerOptions::new()
            .access_inbound(false)
            .access_outbound(true)
            .first_pipe_instance(true)
            .reject_remote_clients(true)
            .create_with_security_attributes_raw(
                name,
                (&mut attributes as *mut SECURITY_ATTRIBUTES).cast(),
            )
    }
}

#[cfg(windows)]
#[allow(unsafe_code)]
async fn deliver(
    mut server: tokio::net::windows::named_pipe::NamedPipeServer,
    key: Key,
) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use tokio::io::AsyncWriteExt;
    use windows_sys::Win32::Storage::FileSystem::FlushFileBuffers;

    server.connect().await?;
    server.write_all(&key.0).await?;
    drop(key);
    // The vendor reads the whole stream before resolving the reference.
    // Flush before closing so the reader receives every byte, then EOF.
    tokio::task::spawn_blocking(move || {
        // SAFETY: server owns the handle for the entire call.
        if unsafe { FlushFileBuffers(server.as_raw_handle()) } == 0 {
            return Err(std::io::Error::last_os_error());
        }
        drop(server);
        Ok(())
    })
    .await?
}
