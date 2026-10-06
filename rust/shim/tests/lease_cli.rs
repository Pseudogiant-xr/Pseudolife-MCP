#![forbid(unsafe_code)]
mod common;
use common::LeaseHome as Home;
use serde_json::Value;
use std::fs;

#[test]
fn check_missing_lock_is_free_and_never_creates_the_lock_file() {
    let home = Home::new();
    let output = home.call(&["lease", "check", "resource", "--json"]);
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stderr.is_empty());
    let json: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(json["held"], false);
    assert!(json["local"]["state"].is_null());
    assert!(!home.0.join("lease-resource.lock").exists());
    assert_eq!(
        fs::read(home.0.join("instance.id")).unwrap().len(),
        if cfg!(windows) { 14 } else { 13 }
    );
}

#[test]
fn ambiguous_run_options_use_the_active_subparser_without_creating_state() {
    let home = Home::new();
    for flag in ["--t", "--t=30", "--t="] {
        let output = home.call(&[
            "lease",
            "run",
            "sample",
            flag,
            "30",
            "--no-board",
            "--",
            "missing-review-child",
        ]);
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        let expected = format!(
            "{}pseudolife-mcp lease run: error: ambiguous option: {flag} could match --ttl, --timeout\n",
            include_str!("../src/cli/lease/assets/run_usage.txt")
        );
        assert_eq!(
            output.stderr,
            expected
                .replace('\n', if cfg!(windows) { "\r\n" } else { "\n" })
                .as_bytes()
        );
        assert_eq!(fs::read_dir(&home.0).unwrap().count(), 0);
    }
}

#[test]
fn suite_holder_accepts_python_iso_date_time_forms() {
    let home = Home::new();
    fs::write(home.0.join("instance.id"), b"0123456789ab\n").unwrap();
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(home.0.join("full-suite.lock"))
        .unwrap();
    file.lock().unwrap();
    for (started, clock) in [
        ("2026-10-05", Some("00:00")),
        ("20261005", Some("00:00")),
        ("2026-W41-1", Some("00:00")),
        ("2026W411", Some("00:00")),
        ("2026-W41", Some("00:00")),
        ("2026W41", Some("00:00")),
        ("2026-10-05T11", Some("11:00")),
        ("2026-10-05 11:12", Some("11:12")),
        ("20261005x1112", Some("11:12")),
        ("2026-10-05😀11:12:13", Some("11:12")),
        ("2026-10-05T111213,123456+01:02:03.4", Some("11:12")),
        ("2026-10-05T11:12:13.0001Z", Some("11:12")),
        ("2026-10-05T11:12+0100", Some("11:12")),
        ("2026-W41-1T11:12:13", Some("11:12")),
        ("2026-10-05T25:00", None),
        ("2026-10-05T11:99", None),
        ("2026-02-30", None),
        ("2026-10- 5", None),
        ("9999-W52-7", None),
        ("2026-10-05T11:12garbage", None),
        ("+026-W01", None),
        ("+026W01", None),
        ("+026-W01T11:12", None),
        ("2026W41111", Some("11:00")),
        ("2026W4111111", Some("11:11")),
        ("2026-W41-1111", Some("11:11")),
        ("2026-10-05T11:12:13:14", Some("11:12")),
        ("2026-10-05T001Z", Some("00:00")),
        ("2026-10-05T11:12Z\0", Some("11:12")),
        ("2026-10-05T11:12Z\0garbage", Some("11:12")),
        ("2026W41111Z\0tail", Some("11:00")),
        ("2026-10-05T11:12Zx", None),
        ("2026-10-05T11:12+01:02\0garbage", None),
    ] {
        fs::write(
            home.0.join("full-suite.holder.json"),
            serde_json::to_vec(
                &serde_json::json!({"pid":42,"worktree":"fixture","started":started}),
            )
            .unwrap(),
        )
        .unwrap();
        let output = home.call(&["lease", "check", "full-suite"]);
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stderr.is_empty());
        let text = String::from_utf8(output.stdout).unwrap();
        let description = format!(
            "held (pid 42, worktree fixture{})",
            clock.map_or(String::new(), |c| format!(", since {c}"))
        );
        assert!(text.contains(&description), "{started:?}: {text}");
    }
}
#[test]
fn check_observes_a_real_external_handle_and_then_its_release() {
    let home = Home::new();
    let path = home.0.join("lease-resource.lock");
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(path)
        .unwrap();
    file.lock().unwrap();
    assert_eq!(
        home.call(&["lease", "check", "resource", "--json"])
            .status
            .code(),
        Some(1)
    );
    drop(file);
    let output = home.call(&["lease", "check", "resource", "--json"]);
    assert_eq!(output.status.code(), Some(0));
    let json: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(json["local"]["state"], "free");
}
#[test]
fn suite_check_scans_slot_one_and_remote_suite_has_no_local_authority() {
    let home = Home::new();
    fs::write(home.0.join("full-suite.lock"), b"").unwrap();
    let slot = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(home.0.join("full-suite.1.lock"))
        .unwrap();
    slot.lock().unwrap();
    fs::write(
        home.0.join("full-suite.1.holder.json"),
        br#"{"pid":17,"worktree":"fixture","started":"2026-10-05T11:12:00"}"#,
    )
    .unwrap();
    let output = home.call(&["lease", "check", "full-suite", "--json"]);
    assert_eq!(output.status.code(), Some(1));
    let json: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(json["local"]["file"], "full-suite.1.lock");
    assert_eq!(json["local"]["pid"], 17);
    assert_eq!(
        home.call(&["lease", "check", "full-suite@peer", "--json"])
            .status
            .code(),
        Some(0)
    );
    drop(slot);
}
#[test]
fn held_zero_timeout_never_starts_the_command_and_nested_run_does_not_wait() {
    let home = Home::new();
    let path = home.0.join("lease-resource.lock");
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(path)
        .unwrap();
    file.lock().unwrap();
    let args = [
        "lease",
        "run",
        "resource",
        "--no-board",
        "--timeout",
        "0",
        "--",
        "missing-native-lease-child",
    ];
    let output = home.call(&args);
    assert_eq!(output.status.code(), Some(75));
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8_lossy(&output.stderr).contains("the command did not run"));
    assert_eq!(
        home.command()
            .args(args)
            .env("PSEUDOLIFE_LEASES_HELD", "outer,resource")
            .output()
            .unwrap()
            .status
            .code(),
        Some(64)
    );
    drop(file);
}
#[test]
fn run_zero_timeout_can_take_a_free_lock_and_missing_child_is_127() {
    let home = Home::new();
    let output = home.call(&[
        "lease",
        "run",
        "resource",
        "--no-board",
        "--timeout",
        "0",
        "--",
        "missing-native-lease-child",
    ]);
    assert_eq!(output.status.code(), Some(127));
    assert!(home.0.join("lease-resource.lock").exists());
    assert_eq!(
        home.call(&["lease", "check", "resource", "--json"])
            .status
            .code(),
        Some(0)
    );
}
#[test]
fn seven_days_is_a_run_expectation_and_operator_actions_remain_deferred() {
    let home = Home::new();
    assert_eq!(
        home.call(&[
            "lease",
            "run",
            "resource",
            "--expect",
            "7d",
            "--no-board",
            "--",
            "missing-native-lease-child"
        ])
        .status
        .code(),
        Some(127)
    );
    for (flag, value) in [("--expect", "8d"), ("--timeout", "1.5h"), ("--for", "7d")] {
        assert_eq!(
            home.call(&[
                "lease",
                "run",
                "resource",
                flag,
                value,
                "--",
                "missing-native-lease-child"
            ])
            .status
            .code(),
            Some(2)
        );
    }
    for action in ["break", "delegate", "hold"] {
        assert_eq!(
            home.call(&["lease", action, "resource"]).status.code(),
            Some(1)
        );
    }
}

fn native_text(value: &str) -> Vec<u8> {
    if cfg!(windows) {
        value.replace('\n', "\r\n").into_bytes()
    } else {
        value.as_bytes().to_vec()
    }
}

#[test]
fn help_clusters_and_explicit_help_errors_match_public_parser_bytes() {
    let home = Home::new();
    for (argv, help) in [
        (
            &["lease", "-hh"][..],
            include_str!("../src/cli/lease/assets/top_help.txt"),
        ),
        (
            &["lease", "check", "-hh"][..],
            include_str!("../src/cli/lease/assets/check_help.txt"),
        ),
    ] {
        let output = home.call(argv);
        assert_eq!(output.status.code(), Some(0));
        assert_eq!(output.stdout, native_text(help));
        assert!(output.stderr.is_empty());
    }
    for (argv, message) in [
        (
            &["lease", "--unknown", "check", "sample"][..],
            "unrecognized arguments: --unknown",
        ),
        (
            &["lease", "--help=x"][..],
            "argument -h/--help: ignored explicit argument 'x'",
        ),
    ] {
        let output = home.call(argv);
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            native_text(&format!(
                "{}pseudolife-mcp lease: error: {message}\n",
                include_str!("../src/cli/lease/assets/top_usage.txt")
            ))
        );
    }
    assert!(
        !home.0.join("instance.id").exists(),
        "parser leaves do not touch lease state"
    );
}

#[test]
fn large_timeout_is_accepted_without_capping_other_duration_options() {
    let home = Home::new();
    for value in [
        "18446744073709551616",
        "18446744073709551616d",
        "9223372036854775808",
    ] {
        let output = home.call(&[
            "lease",
            "run",
            "resource",
            "--no-board",
            "--timeout",
            value,
            "--",
            "missing-boundary-child",
        ]);
        assert_eq!(output.status.code(), Some(127));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            native_text(
                "lease: board skipped: --no-board was given; waiting on the local lock for 'resource' alone (not FIFO)\nlease: command not found: missing-boundary-child\n"
            )
        );
        assert!(
            fs::File::open(home.0.join("lease-resource.lock"))
                .unwrap()
                .try_lock()
                .is_ok()
        );
    }
    for option in ["--ttl", "--expect"] {
        assert_eq!(
            home.call(&[
                "lease",
                "run",
                "resource",
                option,
                "18446744073709551616",
                "--",
                "missing-boundary-child"
            ])
            .status
            .code(),
            Some(2)
        );
    }
}

#[test]
fn nonzero_timeout_still_bounds_acquisition_and_never_starts_child() {
    use std::{
        thread,
        time::{Duration, Instant},
    };
    let home = Home::new();
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(home.0.join("lease-resource.lock"))
        .unwrap();
    file.lock().unwrap();
    let start = Instant::now();
    let mut child = home
        .command()
        .args([
            "lease",
            "run",
            "resource",
            "--no-board",
            "--timeout",
            "1",
            "--",
            "missing-boundary-child",
        ])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    loop {
        if child.try_wait().unwrap().is_some() {
            break;
        }
        if start.elapsed() >= Duration::from_secs(5) {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("one-second acquisition timeout was not enforced");
        }
        thread::sleep(Duration::from_millis(10));
    }
    let output = child.wait_with_output().unwrap();
    assert!(start.elapsed() >= Duration::from_secs(1));
    assert_eq!(output.status.code(), Some(75));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.ends_with("lease: gave up waiting for 'resource' after 1s (--timeout); the command did not run\r\n")
        || stderr.ends_with("lease: gave up waiting for 'resource' after 1s (--timeout); the command did not run\n"));
    assert!(!stderr.contains("command not found"));
    drop(file);
}

#[test]
fn decimal_digit_limit_rejects_before_lock_or_child_start() {
    let home = Home::new();
    let value = "0".repeat(4300) + "1";
    for option in ["--timeout", "--expect", "--ttl"] {
        let output = home.call(&[
            "lease",
            "run",
            "resource",
            option,
            &value,
            "--",
            "missing-boundary-child",
        ]);
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            native_text(&format!(
                "{}pseudolife-mcp lease run: error: argument {option}: Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit\n",
                include_str!("../src/cli/lease/assets/run_usage.txt")
            ))
        );
        assert!(!home.0.join("lease-resource.lock").exists());
        assert!(!home.0.join("instance.id").exists());
    }
}

#[test]
fn overflow_child_sentinel() {
    if let Some(path) = std::env::var_os("LEASE_OVERFLOW_SENTINEL") {
        fs::write(path, b"child-ran").unwrap();
    }
}

#[test]
fn clock_overflow_returns_one_before_any_state_or_child() {
    let home = Home::new();
    let sentinel = home.0.join("child-sentinel");
    let helper = std::env::current_exe().unwrap();
    for value in ["9".repeat(309), "9".repeat(308) + "d"] {
        let output = home
            .command()
            .args([
                "lease",
                "run",
                "resource",
                "--no-board",
                "--timeout",
                &value,
                "--",
            ])
            .arg(&helper)
            .args(["--exact", "overflow_child_sentinel", "--nocapture"])
            .env("LEASE_OVERFLOW_SENTINEL", &sentinel)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert!(!sentinel.exists());
        assert!(!home.0.join("lease-resource.lock").exists());
        assert!(!home.0.join("instance.id").exists());
        assert_eq!(
            output.stderr,
            native_text("OverflowError: int too large to convert to float\n")
        );
    }
}

#[test]
fn enclosing_run_is_rejected_before_clock_overflow() {
    let home = Home::new();
    let sentinel = home.0.join("child-sentinel");
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(home.0.join("lease-resource.lock"))
        .unwrap();
    lock.lock().unwrap();
    let output = home
        .command()
        .args([
            "lease",
            "run",
            "resource",
            "--no-board",
            "--timeout",
            &"9".repeat(309),
            "--",
        ])
        .arg(std::env::current_exe().unwrap())
        .args(["--exact", "overflow_child_sentinel", "--nocapture"])
        .env("LEASE_OVERFLOW_SENTINEL", &sentinel)
        .env("PSEUDOLIFE_LEASES_HELD", "outer,resource")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(64));
    assert_eq!(
        output.stderr,
        native_text(
            "lease: 'resource' is held by an enclosing lease run (PSEUDOLIFE_LEASES_HELD=outer,resource); a nested run of the same lease would wait for itself forever. Run the command directly, or use another name. If this process was not started by that run, unset PSEUDOLIFE_LEASES_HELD.\n"
        )
    );
    assert!(output.stdout.is_empty());
    assert!(!sentinel.exists());
    assert!(!home.0.join("instance.id").exists());
    drop(lock);
    assert_eq!(fs::read(home.0.join("lease-resource.lock")).unwrap(), b"");
}
