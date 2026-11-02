//! A terminal code prompt with echo restored before the result is consumed.
use std::io::{self, Write};

#[cfg(unix)]
#[allow(unsafe_code)]
pub(super) async fn password() -> io::Result<String> {
    use std::{
        io::Read,
        os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
    };
    // Register before hiding echo: the default SIGINT action exits without
    // dropping guards. Async terminal reads let the signal restore the modes
    // without leaving a blocking reader behind.
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    let mut tty = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NONBLOCK)
        .open("/dev/tty")?;
    let input = tokio::io::unix::AsyncFd::new(tty.try_clone()?)?;
    let fd = tty.as_raw_fd();
    let mut previous = std::mem::MaybeUninit::<libc::termios>::uninit();
    // SAFETY: fd is an open terminal and previous is writable termios storage.
    if unsafe { libc::tcgetattr(fd, previous.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let previous = unsafe { previous.assume_init() };
    struct Restore(i32, libc::termios);
    impl Drop for Restore {
        fn drop(&mut self) {
            // SAFETY: the terminal remains open until after this guard drops.
            unsafe {
                libc::tcsetattr(self.0, libc::TCSADRAIN, &self.1);
            }
        }
    }
    let mut quiet = previous;
    quiet.c_lflag &= !libc::ECHO;
    if unsafe { libc::tcsetattr(fd, libc::TCSADRAIN, &quiet) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let restore = Restore(fd, previous);
    tty.write_all(b"pairing code: ")?;
    tty.flush()?;
    let result = async {
        let mut text = Vec::new();
        loop {
            tokio::select! {
                biased;
                _ = interrupt.recv() => return Err(io::ErrorKind::Interrupted.into()),
                ready = input.readable() => {
                    let mut ready = ready?;
                    let mut buffer = [0u8; 1024];
                    match ready.try_io(|inner| inner.get_ref().read(&mut buffer)) {
                        Ok(Ok(count)) => {
                            text.extend_from_slice(&buffer[..count]);
                            if count == 0 || text.contains(&b'\n') {
                                return String::from_utf8(text).map_err(|error|
                                    io::Error::new(io::ErrorKind::InvalidData, error));
                            }
                        }
                        Ok(Err(error)) => return Err(error),
                        Err(_) => continue,
                    }
                }
            }
        }
    }
    .await;
    drop(restore);
    tty.write_all(b"\n")?;
    result
}

#[cfg(windows)]
#[allow(unsafe_code)]
pub(super) async fn password() -> io::Result<String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::Console::{
        ENABLE_ECHO_INPUT, GetConsoleMode, ReadConsoleW, SetConsoleMode,
    };
    let input = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open("CONIN$")?;
    let handle = input.as_raw_handle();
    let mut previous = 0;
    // SAFETY: input owns a console input handle for the duration of the call.
    if unsafe { GetConsoleMode(handle, &mut previous) } == 0 {
        return Err(io::Error::last_os_error());
    }
    struct Restore(windows_sys::Win32::Foundation::HANDLE, u32);
    impl Drop for Restore {
        fn drop(&mut self) {
            unsafe {
                SetConsoleMode(self.0, self.1);
            }
        }
    }
    if unsafe { SetConsoleMode(handle, previous & !ENABLE_ECHO_INPUT) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let restore = Restore(handle, previous);
    let mut stderr = io::stderr().lock();
    stderr.write_all(b"pairing code: ")?;
    stderr.flush()?;
    let mut text = Vec::new();
    loop {
        let mut units = [0u16; 128];
        let mut read = 0;
        let ok = unsafe {
            ReadConsoleW(
                handle,
                units.as_mut_ptr().cast(),
                units.len() as u32,
                &mut read,
                std::ptr::null(),
            )
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        text.extend_from_slice(&units[..read as usize]);
        if read == 0 || text.contains(&10) || text.contains(&13) {
            break;
        }
    }
    drop(restore);
    stderr.write_all(b"\n")?;
    String::from_utf16(&text)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid terminal text"))
}
