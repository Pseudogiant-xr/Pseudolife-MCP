//! Pure tests only: nothing here looks up, starts or reaches a program or a
//! daemon (the session's PATH, Program Files and loopback daemon are real).
use super::*;

fn context() -> Context {
    Context {
        columns_80: true,
        colour_stdout: false,
        colour_stderr: false,
        utf8_argv: true,
    }
}

fn argv(items: &[&str]) -> Vec<String> {
    items.iter().map(|item| (*item).to_owned()).collect()
}

fn answer(items: &[&str]) -> Out {
    decide(&argv(items), &context()).expect("answered")
}

#[test]
fn help_and_usage_errors_need_80_columns_and_no_colour() {
    assert_eq!(answer(&["-h"]).stdout, units(args::MAIN_HELP));
    let narrow = Context {
        columns_80: false,
        ..context()
    };
    assert!(decide(&argv(&["-h"]), &narrow).is_err());
    let coloured = Context {
        colour_stderr: true,
        ..context()
    };
    assert!(decide(&argv(&["bogus"]), &coloured).is_err());
    // Help goes to stdout: a colour setting only on stderr does not matter.
    assert!(decide(&argv(&["status", "-h"]), &coloured).is_ok());
}

#[test]
fn usage_errors_name_the_parser_that_reports_them() {
    let out = answer(&["off", "--port", "0"]);
    assert_eq!(out.code, EXIT_USAGE);
    assert_eq!(
        out.stderr,
        "usage: pseudolife-mcp expose off [-h] [--port PORT] [--yes] [--json]\n\
         pseudolife-mcp expose off: error: argument --port: not a port: '0'\n"
    );
    let out = answer(&["status", "--yes"]);
    assert_eq!(
        out.stderr,
        "usage: pseudolife-mcp expose [-h] {tailscale,off,status} ...\n\
         pseudolife-mcp expose: error: unrecognized arguments: --yes\n"
    );
    assert!(out.stdout.is_empty());
}

#[test]
fn non_utf8_locale_argv_defers_only_when_non_ascii() {
    let posix_c = Context {
        utf8_argv: false,
        ..context()
    };
    assert!(decide(&argv(&["café"]), &posix_c).is_err());
    assert!(decide(&argv(&["bogus"]), &posix_c).is_ok());
}

fn report(action: Action, json: bool) -> Report {
    Report::new(&Args {
        action,
        port: 8443,
        yes: false,
        json,
    })
}

#[test]
fn the_json_report_has_the_oracles_keys_in_order() {
    let mut report = report(Action::Off, true);
    report.tailscale = Some(r"C:\x\tailscale.CMD".into());
    report.command = Some(vec!["tailscale".into(), "serve".into()]);
    let out = report.fail_text(EXIT_USAGE, "no terminal to confirm on: rerun with --yes");
    let text = String::from_utf16(&out.stdout).unwrap();
    assert_eq!(
        text,
        "{\n  \"action\": \"off\",\n  \"port\": 8443,\n  \"tailscale\": \"C:\\\\x\\\\tailscale.CMD\",\n  \"state\": null,\n  \"url\": null,\n  \"detail\": null,\n  \"command\": [\n    \"tailscale\",\n    \"serve\"\n  ],\n  \"daemon\": null,\n  \"probe\": null,\n  \"warnings\": [],\n  \"notes\": [],\n  \"error\": \"no terminal to confirm on: rerun with --yes\",\n  \"exit\": 2\n}\n"
    );
    assert!(out.stderr.is_empty());
}

#[test]
fn a_lone_surrogate_is_escaped_on_stderr_and_in_json() {
    let line = [units("serves a TCP forward to "), vec![0xdc80]].concat();
    let out = report(Action::Off, false).fail(EXIT_REFUSED, &line);
    assert_eq!(out.stderr, "expose: serves a TCP forward to \\udc80\n");
    let out = report(Action::Status, true).fail(EXIT_REFUSED, &line);
    assert!(
        String::from_utf16(&out.stdout)
            .unwrap()
            .contains("\"error\": \"serves a TCP forward to \\udc80\"")
    );
    // On stdout without --json, run() writes it escaped (`_write`).
    let mut text = report(Action::Status, false);
    text.say(&line);
    assert_eq!(
        pyjson::backslashreplace(&text.finish(EXIT_OK).stdout),
        "serves a TCP forward to \\udc80\n"
    );
}
