#![forbid(unsafe_code)]
use std::{fs, process::Command};

#[test]
fn explicit_unbuffered_stdout_fails_directly_without_shutdown_retry() {
    let home = std::env::temp_dir().join(format!("wait-mail-unbuffered-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let digest = home.join(format!("{}.txt", "a".repeat(64)));
    fs::write(&digest, b"1\npeer\n").unwrap();
    fs::write(digest.with_extension("ring"), b"1\nrung anyone\n").unwrap();
    let mut results = Vec::new();
    for setting in ["", "0", "1"] {
        for help in [false, true] {
            let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
            command.arg("wait-mail");
            if help {
                command.arg("--help");
            } else {
                command.arg("--digest").arg(&digest).args([
                    "--timeout",
                    "0.05",
                    "--interval",
                    "0.01",
                ]);
            }
            let output = command
                .env_clear()
                .env("PYTHONUNBUFFERED", setting)
                .env("HOME", &home)
                .env("USERPROFILE", &home)
                .stdout(fs::File::open(&digest).unwrap())
                .output()
                .unwrap();
            let count = fs::read_dir(&home).unwrap().count();
            results.push((setting, help, output, count));
        }
    }
    fs::remove_dir_all(home).unwrap();
    for (setting, help, output, count) in results {
        let expected = 2;
        assert_eq!(
            output.status.code(),
            Some(expected),
            "setting={setting:?}, help={help}"
        );
        assert_eq!(count, 2);
        assert!(
            !String::from_utf8(output.stderr.clone())
                .unwrap()
                .contains("Exception ignored in:")
        );
        if help {
            assert!(
                String::from_utf8(output.stderr)
                    .unwrap()
                    .contains("could not write help to stdout")
            );
        }
    }
}

#[cfg(unix)]
#[test]
fn opaque_digest_parent_errors_keep_surrogateescape_without_side_effects() {
    use std::os::unix::ffi::OsStringExt;
    let home = std::env::temp_dir().join(format!("wait-mail-path-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let parent = home.join(std::ffi::OsString::from_vec(b"parent\xff".to_vec()));
    let digest = parent.join(format!("{}.txt", "a".repeat(64)));
    let mut results = Vec::new();
    for present in [false, true] {
        if present {
            fs::write(&parent, b"blocker").unwrap();
        }
        let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .args(["wait-mail", "--digest"])
            .arg(&digest)
            .env_clear()
            .output()
            .unwrap();
        results.push((present, output, fs::read_dir(&home).unwrap().count()));
    }
    fs::remove_dir_all(home).unwrap();
    for (present, output, count) in results {
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(count, usize::from(present));
        let error = String::from_utf8(output.stderr).unwrap();
        assert!(error.contains("parent\\udcff/"), "{error}");
        assert!(!error.contains('\u{fffd}'));
        if present {
            assert!(error.contains("[Errno 20] Not a directory"));
        }
    }
}

#[test]
fn failed_stderr_aborts_delivery_before_stdout_and_durable_state() {
    let home = std::env::temp_dir().join(format!("wait-mail-stderr-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let digest = home.join(format!("{}.txt", "a".repeat(64)));
    fs::write(&digest, b"1\npeer\n").unwrap();
    fs::write(digest.with_extension("ring"), b"1\nrung anyone\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .args(["wait-mail", "--digest"])
        .arg(&digest)
        .args(["--timeout", "0.05", "--interval", "0.01"])
        .env_clear()
        .env("HOME", &home)
        .env("USERPROFILE", &home)
        .stderr(fs::File::open(&digest).unwrap())
        .output()
        .unwrap();
    let count = fs::read_dir(&home).unwrap().count();
    fs::remove_dir_all(home).unwrap();
    assert_eq!(output.status.code(), Some(120));
    assert!(output.stdout.is_empty());
    assert_eq!(count, 2);
}

#[test]
fn stdout_failures_leave_mail_unshown_with_direct_exit_2() {
    let home = std::env::temp_dir().join(format!("wait-mail-stdout-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let digest = home.join(format!("{}.txt", "a".repeat(64)));
    let mut results = Vec::new();
    let buffer_size = if cfg!(windows) { 8192 } else { 4096 };
    for (size, expected) in [(5, 2), (buffer_size, 2), (buffer_size + 1, 2)] {
        fs::write(
            &digest,
            [b"1\n".as_slice(), vec![b'p'; size].as_slice()].concat(),
        )
        .unwrap();
        fs::write(digest.with_extension("ring"), b"1\nrung anyone\n").unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .args(["wait-mail", "--digest"])
            .arg(&digest)
            .args(["--timeout", "0.05", "--interval", "0.01"])
            .env_clear()
            .env("HOME", &home)
            .env("USERPROFILE", &home)
            .stdout(fs::File::open(&digest).unwrap())
            .output()
            .unwrap();
        results.push((
            output.status.code(),
            expected,
            String::from_utf8(output.stderr)
                .unwrap()
                .contains("Exception ignored in:"),
        ));
    }
    let help = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .args(["wait-mail", "--help"])
        .env_clear()
        .stdout(fs::File::open(&digest).unwrap())
        .output()
        .unwrap();
    let count = fs::read_dir(&home).unwrap().count();
    fs::remove_dir_all(home).unwrap();
    for (actual, expected, shutdown) in results {
        assert_eq!(actual, Some(expected));
        assert!(!shutdown);
    }
    assert_eq!(help.status.code(), Some(2));
    assert_eq!(count, 2);
}

#[test]
fn help_does_not_bypass_later_ambiguous_option() {
    let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .args(["wait-mail", "--help", "--=x"])
        .env_clear()
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8(output.stderr).unwrap().contains(
        "ambiguous option: --=x could match --help, --session-id, --digest, --timeout, --interval"
    ));
}

#[cfg(unix)]
#[test]
fn opaque_unknown_option_and_float_are_escaped_without_replacement() {
    use std::{ffi::OsString, os::unix::ffi::OsStringExt};
    for (arguments, ending) in [
        (
            vec![OsString::from_vec(b"--bogus\xff".to_vec())],
            "unrecognized arguments: --bogus\\udcff\n",
        ),
        (
            vec![
                OsString::from("--timeout"),
                OsString::from_vec(b"bad\xff".to_vec()),
            ],
            "argument --timeout: invalid float value: 'bad\\udcff'\n",
        ),
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
            .arg("wait-mail")
            .args(arguments)
            .env_clear()
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(
            output.stderr.ends_with(ending.as_bytes()),
            "{:?}",
            output.stderr
        );
    }
}

#[cfg(unix)]
#[test]
fn fifo_replacement_is_interruptible_and_listener_permissions_override_umask() {
    use std::{
        os::unix::fs::PermissionsExt,
        process::Stdio,
        time::{Duration, Instant},
    };
    let home = std::env::temp_dir().join(format!("wait-mail-fifo-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let digest = home.join(format!("{}.txt", "a".repeat(64)));
    fs::write(&digest, b"1\npeer\n").unwrap();
    let mut child = Command::new("/bin/sh")
        .args([
            "-c",
            "umask 0777; exec \"$@\"",
            "wait-mail-test",
            env!("CARGO_BIN_EXE_pseudolife-stdio"),
            "wait-mail",
            "--digest",
        ])
        .arg(&digest)
        .args(["--timeout", "5", "--interval", "0.02"])
        .env_clear()
        .env("HOME", &home)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let listener = loop {
        let found = fs::read_dir(&home)
            .unwrap()
            .map(|path| path.unwrap().path())
            .find(|path| {
                path.extension()
                    .is_some_and(|extension| extension == "wait-armed")
                    && !path.file_name().unwrap().to_string_lossy().starts_with('.')
            });
        if let Some(path) = found {
            break Some(path);
        }
        if Instant::now() >= deadline {
            break None;
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    let mode = listener
        .as_ref()
        .map(|path| fs::metadata(path).unwrap().permissions().mode() & 0o777);
    fs::remove_file(&digest).unwrap();
    rustix::fs::mkfifoat(
        rustix::fs::CWD,
        &digest,
        rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
    )
    .unwrap();
    std::thread::sleep(Duration::from_millis(150));
    rustix::process::kill_process(
        rustix::process::Pid::from_raw(child.id() as i32).unwrap(),
        rustix::process::Signal::INT,
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut forced = false;
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            child.kill().unwrap();
            forced = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    let output = child.wait_with_output().unwrap();
    let listener_left = listener.is_some_and(|path| path.exists());
    fs::remove_dir_all(home).unwrap();
    assert_eq!(mode, Some(0o600));
    assert!(!forced);
    assert_eq!(output.status.code(), Some(130));
    assert!(!listener_left);
}

#[test]
fn failed_seen_rename_uses_python_temporary_name_shape_and_cleans_up() {
    let home = std::env::temp_dir().join(format!("wait-mail-temp-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let digest = home.join(format!("{}.txt", "a".repeat(64)));
    fs::write(&digest, b"1\npeer\n").unwrap();
    fs::write(digest.with_extension("ring"), b"1\nrung anyone\n").unwrap();
    fs::create_dir(digest.with_extension("seen")).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .args(["wait-mail", "--digest"])
        .arg(&digest)
        .env_clear()
        .env("HOME", &home)
        .env("USERPROFILE", &home)
        .output()
        .unwrap();
    let text = String::from_utf8(output.stderr).unwrap();
    let suffix = text
        .split(".tmp-")
        .nth(1)
        .unwrap()
        .split(".seen")
        .next()
        .unwrap();
    let actual = (
        output.status.code(),
        suffix.len(),
        suffix
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_'),
    );
    let leftovers = fs::read_dir(&home)
        .unwrap()
        .filter(|p| {
            p.as_ref()
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".tmp-")
        })
        .count();
    fs::remove_dir_all(home).unwrap();
    assert_eq!(actual, (Some(0), 8, true), "{text}");
    assert_eq!(leftovers, 0);
}

#[cfg(unix)]
#[test]
fn opaque_session_bytes_refuse_without_digest_or_listener_side_effects() {
    use std::{ffi::OsString, os::unix::ffi::OsStringExt};
    let home = std::env::temp_dir().join(format!("wait-mail-opaque-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let mut outputs = Vec::new();
    for (raw, inline, environment, detail) in [
        (
            b"peer\xff".as_slice(),
            false,
            false,
            "character '\\udcff' in position 4",
        ),
        (
            b"caf\xc3\xa9\xff\xfe".as_slice(),
            true,
            false,
            "characters in position 4-5",
        ),
        (
            b"peer\xff".as_slice(),
            false,
            true,
            "character '\\udcff' in position 4",
        ),
    ] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"));
        command.arg("wait-mail").env_clear().env("HOME", &home);
        let value = OsString::from_vec(raw.to_vec());
        if environment {
            command.env("CLAUDE_CODE_SESSION_ID", value);
        } else if inline {
            command.arg(OsString::from_vec(
                [b"--session-id=".as_slice(), raw].concat(),
            ));
        } else {
            command.arg("--session-id").arg(value);
        }
        let output = command.output().unwrap();
        outputs.push((output, detail));
    }
    let files = fs::read_dir(&home).unwrap().count();
    fs::remove_dir_all(home).unwrap();
    for (output, detail) in outputs {
        assert_eq!(output.status.code(), Some(1), "{:?}", output.stderr);
        assert!(output.stdout.is_empty());
        let terminal = format!(
            "UnicodeEncodeError: 'utf-8' codec can't encode {detail}: surrogates not allowed\n"
        );
        assert!(
            output.stderr.ends_with(terminal.as_bytes()),
            "{:?}",
            output.stderr
        );
    }
    assert_eq!(files, 0);
}

#[test]
fn rung_mail_uses_binary_stdout_then_advances_seen_and_cleans_listener() {
    let home = std::env::temp_dir().join(format!("wait-mail-native-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&home).unwrap();
    let digest = home.join(format!("{}.txt", "a".repeat(64)));
    let body = "peer — café 🧠\n".as_bytes();
    fs::write(&digest, [b"12\n".as_slice(), body].concat()).unwrap();
    fs::write(digest.with_extension("ring"), b"13\nrung anyone\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .args(["wait-mail", "--digest"])
        .arg(&digest)
        .args(["--timeout", "0.05", "--interval", "0.01"])
        .env_clear()
        .env("USERPROFILE", &home)
        .env("HOME", &home)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0), "{:?}", output.stderr);
    assert_eq!(output.stdout, body);
    assert_eq!(fs::read(digest.with_extension("seen")).unwrap(), b"12\n");
    assert!(
        String::from_utf8(output.stderr)
            .unwrap()
            .contains("(rung anyone, watermark 12, 0 s after arming):")
    );
    assert_eq!(fs::read_dir(&home).unwrap().count(), 4);
    fs::remove_dir_all(home).unwrap();
}
