#![forbid(unsafe_code)]
mod common;
use common::LeaseHome as Home;
fn native(text: &str) -> Vec<u8> {
    text.replace('\n', if cfg!(windows) { "\r\n" } else { "\n" })
        .into_bytes()
}
#[test]
fn new_help_assets_are_exact_and_designate_uses_delegate_parser() {
    for (action, asset) in [
        (
            "hold",
            include_str!("../src/cli/lease/assets/hold_help.txt"),
        ),
        (
            "break",
            include_str!("../src/cli/lease/assets/break_help.txt"),
        ),
        (
            "delegate",
            include_str!("../src/cli/lease/assets/delegate_help.txt"),
        ),
    ] {
        let home = Home::new();
        let output = home.call(&["lease", action, "--help"]);
        assert_eq!(output.status.code(), Some(0));
        assert!(output.stderr.is_empty());
        assert_eq!(output.stdout, native(asset));
        assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
    }
    let home = Home::new();
    let output = home.call(&["lease", "designate", "--help"]);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        output.stdout,
        native(include_str!("../src/cli/lease/assets/delegate_help.txt"))
    );
    assert_eq!(
        output.stderr,
        native("pseudolife-mcp lease designate is deprecated: use pseudolife-mcp lease delegate\n")
    );
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
}
#[test]
fn required_arguments_and_operator_duration_errors_precede_effects() {
    for (arguments, usage, error) in [
        (
            vec!["hold"],
            include_str!("../src/cli/lease/assets/hold_usage.txt"),
            "the following arguments are required: NAME, --while-pid",
        ),
        (
            vec!["hold", "resource"],
            include_str!("../src/cli/lease/assets/hold_usage.txt"),
            "the following arguments are required: --while-pid",
        ),
        (
            vec!["hold", "resource", "--while-pid", "0"],
            include_str!("../src/cli/lease/assets/hold_usage.txt"),
            "argument --while-pid: a process id is a positive whole number",
        ),
        (
            vec!["hold", "resource", "--while-pid", "-1"],
            include_str!("../src/cli/lease/assets/hold_usage.txt"),
            "argument --while-pid: a process id is a positive whole number",
        ),
        (
            vec!["hold", "resource", "--while-pid", "not-pid"],
            include_str!("../src/cli/lease/assets/hold_usage.txt"),
            "argument --while-pid: not a process id: 'not-pid'",
        ),
        (
            vec!["break"],
            include_str!("../src/cli/lease/assets/break_usage.txt"),
            "the following arguments are required: NAME",
        ),
        (
            vec!["delegate"],
            include_str!("../src/cli/lease/assets/delegate_usage.txt"),
            "the following arguments are required: PROJECT, AGENT",
        ),
        (
            vec!["delegate", "project", "agent", "--for", "59"],
            include_str!("../src/cli/lease/assets/delegate_usage.txt"),
            "argument --for: '59' is out of range: from 60 to 604800 seconds",
        ),
        (
            vec!["delegate", "project", "agent", "--for", "8d"],
            include_str!("../src/cli/lease/assets/delegate_usage.txt"),
            "argument --for: '8d' is out of range: from 60 to 604800 seconds",
        ),
    ] {
        let home = Home::new();
        let mut args = vec!["lease"];
        args.extend(arguments.iter().copied());
        let output = home.call(&args);
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            native(&format!(
                "{usage}pseudolife-mcp lease {}: error: {error}\n",
                arguments[0]
            ))
        );
        assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
    }
}
#[test]
fn hold_rejects_command_separator_and_preserves_unicode_names() {
    let home = Home::new();
    let completed = home.completed_pid();
    let pid = completed.id().to_string();
    let output = home.call(&[
        "lease",
        "hold",
        "资源",
        "--while-pid",
        &pid,
        "--",
        "unexpected",
    ]);
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        native(&format!(
            "{}pseudolife-mcp lease hold: error: hold takes no command\n",
            include_str!("../src/cli/lease/assets/hold_usage.txt")
        ))
    );
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
    let output = home.call(&["lease", "hold", "资源", "--while-pid", &pid]);
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        native(&format!(
            "lease: pid {pid} is already gone; nothing to hold '资源' for\n"
        ))
    );
    assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
    drop(completed);
}
