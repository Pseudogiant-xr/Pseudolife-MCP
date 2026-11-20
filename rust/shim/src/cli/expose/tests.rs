//! In-process tests only: nothing here looks up, starts or reaches a real
//! program or daemon (the session's PATH, Program Files, loopback daemon
//! and Tailscale are real). The flows run against [`Fake`], a scripted
//! `tailscale`, daemon and terminal.
use super::*;
use serde_json::{Value, json};
use std::cell::{Cell, RefCell};

fn argv(items: &[&str]) -> Vec<String> {
    items.iter().map(|item| (*item).to_owned()).collect()
}

/// A run that never reaches the host (help, usage errors).
fn answer(items: &[&str]) -> Out {
    decide(&argv(items), &Fake::free())
}

// ── the fake host ────────────────────────────────────────────────────────────

const PORT: &str = "8443";
const LOCAL: &str = "http://127.0.0.1:8443";
const URL: &str = "http://100.64.0.7:8443";
const BANK: &str = "bank-1";

/// What a mutating call does to the serve state.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum Effect {
    /// What it was asked: `serve --bg` adds the forward, `off` removes it.
    #[default]
    Asked,
    /// Nothing at all.
    Nothing,
    /// `serve --bg` adds a forward that also terminates TLS.
    Tls,
}

/// A mutating call's answer and effect.
#[derive(Clone, Debug, Default)]
struct Step {
    code: i64,
    out: String,
    err: String,
    timed_out: bool,
    effect: Effect,
}

struct Fake {
    serve: RefCell<Value>,
    status: String,
    ip4: String,
    bg: Step,
    off: Step,
    /// `serve status --json` fails once anything was changed.
    unreadable_after_change: bool,
    exposure_unsettled: bool,
    changed: Cell<bool>,
    local: Health,
    tailnet: Health,
    terminal: bool,
    answer: Answer,
    /// The serve state someone else leaves while the question waits.
    meanwhile: Option<Value>,
    /// The serve state someone else leaves just after a removal.
    meanwhile_off: Option<Value>,
    interrupt_during_change: bool,
    holding: Cell<bool>,
    interrupted: Cell<bool>,
    calls: RefCell<Vec<Vec<String>>>,
    shown: RefCell<Vec<u8>>,
    asked: RefCell<Vec<String>>,
}

fn healthy() -> Health {
    match json_dict(r#"{"status": "ok", "auth": true, "bank": "bank-1"}"#) {
        J::Dict(dict) => Health::Ok(dict),
        other => panic!("{other:?}"),
    }
}

fn json_dict(text: &str) -> J {
    pyjson::loads(text, tailscale::DEPTH).expect("JSON")
}

impl Fake {
    /// A healthy authenticated daemon, Tailscale running, port 8443 free.
    fn free() -> Self {
        Self {
            serve: RefCell::new(json!({})),
            status: r#"{"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.7"]}}"#
                .into(),
            ip4: "100.64.0.7\n".into(),
            bg: Step::default(),
            off: Step::default(),
            unreadable_after_change: false,
            exposure_unsettled: false,
            changed: Cell::new(false),
            local: healthy(),
            tailnet: healthy(),
            terminal: false,
            answer: Answer::No,
            meanwhile: None,
            meanwhile_off: None,
            interrupt_during_change: false,
            holding: Cell::new(false),
            interrupted: Cell::new(false),
            calls: RefCell::new(Vec::new()),
            shown: RefCell::new(Vec::new()),
            asked: RefCell::new(Vec::new()),
        }
    }

    /// This daemon's forward on 8443 already in place.
    fn ours() -> Self {
        let fake = Self::free();
        *fake.serve.borrow_mut() = json!({"TCP": {PORT: {"TCPForward": "127.0.0.1:8443"}}});
        fake
    }

    fn with_serve(self, serve: Value) -> Self {
        *self.serve.borrow_mut() = serve;
        self
    }

    fn at_terminal(mut self, answer: Answer) -> Self {
        self.terminal = true;
        self.answer = answer;
        self
    }

    fn calls(&self) -> Vec<Vec<String>> {
        self.calls.borrow().clone()
    }

    fn mutations(&self) -> Vec<Vec<String>> {
        self.calls()
            .into_iter()
            .filter(|call| call.first().map(String::as_str) == Some("serve") && call[1] != "status")
            .collect()
    }

    fn serve_state(&self) -> Value {
        self.serve.borrow().clone()
    }

    fn apply(&self, step: &Step, adds: bool) -> Proc {
        self.changed.set(true);
        if self.interrupt_during_change && self.holding.get() {
            self.interrupted.set(true);
        }
        let mut serve = self.serve.borrow_mut();
        let tcp = serve
            .as_object_mut()
            .expect("a config object")
            .entry("TCP")
            .or_insert_with(|| json!({}));
        match (step.effect, adds) {
            (Effect::Asked, true) => {
                tcp[PORT] = json!({"TCPForward": "127.0.0.1:8443"});
            }
            (Effect::Tls, true) => {
                tcp[PORT] =
                    json!({"TCPForward": "127.0.0.1:8443", "TerminateTLS": "node.example.com"});
            }
            (Effect::Asked, false) => {
                tcp.as_object_mut().expect("TCP").remove(PORT);
            }
            _ => {}
        }
        if tcp.as_object().is_some_and(serde_json::Map::is_empty) {
            serve.as_object_mut().expect("object").remove("TCP");
        }
        Proc {
            code: step.code,
            out: step.out.clone(),
            err: step.err.clone(),
            unreadable: false,
            timed_out: step.timed_out,
            err_before: String::new(),
            settled: !self.exposure_unsettled,
        }
    }
}

fn ok(out: &str) -> Proc {
    Proc {
        out: out.to_owned(),
        ..Proc::default()
    }
}

impl Host for Fake {
    fn find(&self) -> Option<Binary> {
        Some(Binary {
            path: "/fake/tailscale".into(),
            shown: "/fake/tailscale".into(),
        })
    }

    async fn run(&self, _binary: &Binary, args: &[&str]) -> Proc {
        let call: Vec<String> = args.iter().map(|word| (*word).to_owned()).collect();
        self.calls.borrow_mut().push(call);
        match args {
            ["status", "--json"] => ok(&self.status),
            ["ip", "-4"] => ok(&self.ip4),
            ["serve", "status", "--json"] if self.unreadable_after_change && self.changed.get() => {
                Proc {
                    code: 1,
                    err: "connection refused\n".into(),
                    ..Proc::default()
                }
            }
            ["serve", "status", "--json"] => ok(&self.serve.borrow().to_string()),
            ["serve", "--bg", "--tcp=8443", "tcp://127.0.0.1:8443"] => {
                self.apply(&self.bg.clone(), true)
            }
            ["serve", "--tcp=8443", "off"] => {
                let proc = self.apply(&self.off.clone(), false);
                if let Some(serve) = &self.meanwhile_off {
                    *self.serve.borrow_mut() = serve.clone();
                }
                proc
            }
            other => panic!("unexpected tailscale call {other:?}"),
        }
    }

    async fn health(&self, origin: &str, _timeout: Duration) -> Health {
        if origin == LOCAL {
            self.local.clone()
        } else {
            assert_eq!(origin, URL, "the probe goes to the tailnet address");
            self.tailnet.clone()
        }
    }

    fn interactive(&self) -> bool {
        self.terminal
    }

    fn show(&self, stdout: &[u8], stderr: &[u8]) -> bool {
        self.shown.borrow_mut().extend_from_slice(stdout);
        self.shown.borrow_mut().extend_from_slice(stderr);
        true
    }

    async fn ask(&self, question: &str) -> Answer {
        assert!(
            self.mutations().is_empty(),
            "asked after a change: {:?}",
            self.calls()
        );
        self.asked.borrow_mut().push(question.to_owned());
        if let Some(serve) = &self.meanwhile {
            *self.serve.borrow_mut() = serve.clone();
        }
        self.answer
    }

    fn hold_interrupts(&self) {
        self.holding.set(true);
    }

    fn interrupted(&self) -> bool {
        self.interrupted.get()
    }
}

/// Run `expose <items>` against `fake`: (stdout, stderr, exit).
fn drive(fake: &Fake, items: &[&str]) -> (String, String, u8) {
    let out = decide(&argv(items), fake);
    (
        String::from_utf16(&out.stdout).expect("stdout text"),
        out.stderr,
        out.code,
    )
}

fn words(items: &[&str]) -> Vec<String> {
    argv(items)
}

fn reads_then(mutation: &[&str]) -> Vec<Vec<String>> {
    vec![
        words(&["status", "--json"]),
        words(&["serve", "status", "--json"]),
        words(&["ip", "-4"]),
        words(mutation),
        words(&["serve", "status", "--json"]),
    ]
}

const SERVE: [&str; 4] = ["serve", "--bg", "--tcp=8443", "tcp://127.0.0.1:8443"];
const OFF: [&str; 3] = ["serve", "--tcp=8443", "off"];

fn plan() -> String {
    format!(
        "plan:\n  run      tailscale serve --bg --tcp=8443 tcp://127.0.0.1:8443\n  clients  {URL}\n"
    )
}

// ── tailscale ────────────────────────────────────────────────────────────────

#[test]
fn tailscale_yes_runs_exactly_the_serve_then_verifies_and_reports_the_address() {
    let fake = Fake::free();
    let (stdout, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_OK, "{stderr}");
    assert_eq!(fake.calls(), reads_then(&SERVE));
    assert_eq!(
        fake.serve_state(),
        json!({"TCP": {PORT: {"TCPForward": "127.0.0.1:8443"}}})
    );
    assert_eq!(
        stdout,
        format!(
            "{}exposed: clients use {URL}\nchecked: {URL}/health answers with the same bank ({BANK})\nnext, for each joining machine: run `pseudolife-mcp invite <machine>` here, then give the code it prints to that machine's installer (option 2), or run `pseudolife-mcp pair {URL} <code>` there\nto remove the forward: pseudolife-mcp expose off --port {PORT}\n",
            plan()
        )
    );
    assert_eq!(stderr, "");
    assert!(fake.holding.get(), "interrupts are held during the change");
}

#[test]
fn tailscale_yes_json_reports_the_verified_state_probe_and_notes() {
    let fake = Fake::free();
    let (stdout, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes", "--json"]);
    assert_eq!((code, stderr.as_str()), (EXIT_OK, ""));
    let report: Value = serde_json::from_str(&stdout).expect("one JSON report");
    assert_eq!(report["state"], "exposed");
    assert_eq!(report["url"], URL);
    assert_eq!(
        report["probe"],
        json!({"reachable": true, "same_bank": true})
    );
    assert_eq!(report["notes"].as_array().map(Vec::len), Some(2));
    assert_eq!(report["warnings"], json!([]));
    assert_eq!(report["error"], Value::Null);
    assert_eq!(report["exit"], 0);
    assert_eq!(
        report["command"],
        json!([
            "tailscale",
            "serve",
            "--bg",
            "--tcp=8443",
            "tcp://127.0.0.1:8443"
        ])
    );
}

#[test]
fn a_probe_that_cannot_reach_the_address_warns_and_changes_nothing_back() {
    let mut fake = Fake::free();
    fake.tailnet = Health::None;
    let (stdout, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_OK);
    assert!(stdout.contains(&format!("exposed: clients use {URL}\n")));
    assert_eq!(
        stderr,
        format!(
            "this host could not reach {URL}/health through the tailnet, which is common for a host's own serve address; check from another machine: curl {URL}/health\n"
        )
    );
    assert_eq!(fake.mutations(), vec![words(&SERVE)], "never rolled back");
    let mut other = Fake::free();
    other.tailnet = match json_dict(r#"{"status": "ok", "bank": "bank-2"}"#) {
        J::Dict(dict) => Health::Ok(dict),
        _ => unreachable!(),
    };
    let (_, stderr, code) = drive(&other, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_OK);
    assert_eq!(
        stderr,
        format!(
            "{URL}/health answers with a different bank (bank-2, this daemon has {BANK}); something else may be serving that address\n"
        )
    );
}

#[test]
fn a_rerun_on_an_exposed_host_changes_nothing_and_says_so_in_one_line() {
    let fake = Fake::ours();
    let (stdout, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(
        (stdout.as_str(), stderr.as_str(), code),
        (
            "already exposed: clients use http://100.64.0.7:8443\n",
            "",
            EXIT_OK
        )
    );
    assert!(fake.mutations().is_empty());
}

#[test]
fn at_a_terminal_it_shows_the_plan_asks_once_and_goes_ahead_only_on_yes() {
    let fake = Fake::free().at_terminal(Answer::Yes);
    let (stdout, _, code) = drive(&fake, &["tailscale", "--port", PORT]);
    assert_eq!(code, EXIT_OK);
    assert_eq!(
        fake.asked.borrow().as_slice(),
        [format!(
            "expose this daemon on the tailnet at {URL}? [y/N] "
        )]
    );
    // The plan was written before the question, and not again after it.
    assert_eq!(
        String::from_utf8(fake.shown.borrow().clone()).unwrap(),
        String::from_utf8(super::super::text_bytes(&plan())).unwrap()
    );
    assert!(stdout.starts_with(&format!("exposed: clients use {URL}\n")));
    assert_eq!(fake.mutations(), vec![words(&SERVE)]);
    // The port is read again after the answer, before the change.
    assert_eq!(
        fake.calls()[3..5],
        [words(&["serve", "status", "--json"]), words(&SERVE)]
    );
}

#[test]
fn a_port_taken_while_the_question_waits_is_never_replaced() {
    let mut fake = Fake::free().at_terminal(Answer::Yes);
    let theirs = json!({"TCP": {PORT: {"HTTPS": true}}});
    fake.meanwhile = Some(theirs.clone());
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT]);
    assert_eq!(code, EXIT_REFUSED);
    assert_eq!(
        stderr,
        "expose: tailnet port 8443 changed while the question waited: it now serves HTTPS; nothing changed. Rerun to see the plan for it now\n"
    );
    assert!(fake.mutations().is_empty());
    assert_eq!(fake.serve_state(), theirs);
}

#[test]
fn a_forward_replaced_while_off_asks_is_never_removed() {
    let mut fake = Fake::ours().at_terminal(Answer::Yes);
    let theirs = json!({"TCP": {PORT: {"TCPForward": "127.0.0.1:1"}}});
    fake.meanwhile = Some(theirs.clone());
    let (_, stderr, code) = drive(&fake, &["off", "--port", PORT]);
    assert_eq!(code, EXIT_REFUSED);
    assert!(
        stderr.contains("it now serves a TCP forward to 127.0.0.1:1; nothing changed"),
        "{stderr}"
    );
    assert!(fake.mutations().is_empty());
    assert_eq!(fake.serve_state(), theirs);
    // Removed by someone else meanwhile: the goal is met, in one line.
    let mut fake = Fake::ours().at_terminal(Answer::Yes);
    fake.meanwhile = Some(json!({}));
    let (stdout, stderr, code) = drive(&fake, &["off", "--port", PORT]);
    assert_eq!((code, stderr.as_str()), (EXIT_OK, ""));
    assert!(
        stdout.ends_with("not exposed: nothing serves tailnet port 8443\n"),
        "{stdout}"
    );
    assert!(fake.mutations().is_empty());
}

#[test]
fn a_forward_put_in_place_while_the_question_waits_is_already_exposed() {
    let mut fake = Fake::free().at_terminal(Answer::Yes);
    fake.meanwhile = Some(json!({"TCP": {PORT: {"TCPForward": "127.0.0.1:8443"}}}));
    let (stdout, stderr, code) = drive(&fake, &["tailscale", "--port", PORT]);
    assert_eq!((code, stderr.as_str()), (EXIT_OK, ""));
    assert_eq!(stdout, format!("already exposed: clients use {URL}\n"));
    assert!(fake.mutations().is_empty());
}

#[test]
fn a_timed_out_serve_that_printed_no_link_names_the_check_to_make() {
    let mut fake = Fake::free();
    fake.bg = Step {
        code: 124,
        err: "timed out after 30s".into(),
        timed_out: true,
        effect: Effect::Nothing,
        ..Step::default()
    };
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_REFUSED);
    assert_eq!(
        stderr,
        "expose: `tailscale serve` did not finish within 30s; nothing changed. Check that Tailscale answers (`tailscale status`), then rerun `pseudolife-mcp expose tailscale --port 8443`\n"
    );
}

#[test]
fn a_declined_or_unanswered_question_changes_nothing_and_says_so() {
    // `ask` reads an answer other than y/yes, and end of input, as no.
    let fake = Fake::free().at_terminal(Answer::No);
    let (stdout, stderr, code) = drive(&fake, &["tailscale", "--port", PORT]);
    assert_eq!(code, EXIT_USAGE);
    assert_eq!(stdout, "");
    assert_eq!(stderr, format!("expose: {NOT_CONFIRMED}\n"));
    assert!(fake.mutations().is_empty());
    assert_eq!(fake.serve_state(), json!({}));
    assert_eq!(fake.asked.borrow().len(), 1);
}

#[test]
fn an_interrupt_at_the_question_changes_nothing_and_says_so() {
    let fake = Fake::free().at_terminal(Answer::Interrupted);
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--json"]);
    assert_eq!(code, EXIT_INTERRUPTED);
    assert_eq!(stderr, "");
    assert!(fake.mutations().is_empty());
    let fake = Fake::ours().at_terminal(Answer::Interrupted);
    let (_, stderr, code) = drive(&fake, &["off", "--port", PORT]);
    assert_eq!(
        (code, stderr),
        (EXIT_INTERRUPTED, format!("expose: {INTERRUPTED}\n"))
    );
    assert!(fake.mutations().is_empty());
}

#[test]
fn without_a_terminal_or_yes_it_refuses_naming_yes_and_changes_nothing() {
    for (fake, action) in [(Fake::free(), "tailscale"), (Fake::ours(), "off")] {
        let (_, stderr, code) = drive(&fake, &[action, "--port", PORT]);
        assert_eq!(code, EXIT_USAGE, "{action}");
        assert_eq!(stderr, format!("expose: {NO_TERMINAL}\n"));
        assert!(fake.mutations().is_empty());
        assert!(
            fake.asked.borrow().is_empty(),
            "never asks without a terminal"
        );
    }
}

#[test]
fn a_foreign_serve_on_the_port_is_named_and_never_replaced() {
    let https = json!({"TCP": {PORT: {"HTTPS": true}}, "Web": {"n:443": {}}});
    let fake = Fake::free().with_serve(https.clone());
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_REFUSED);
    assert!(
        stderr.starts_with(
            "expose: tailnet port 8443 already serves HTTPS; expose never replaces another serve"
        ),
        "{stderr}"
    );
    assert!(fake.mutations().is_empty());
    assert_eq!(fake.serve_state(), https);
}

#[test]
fn forwards_on_other_ports_survive_the_serve() {
    let other = json!({"TCP": {"9000": {"TCPForward": "127.0.0.1:9000"}}});
    let fake = Fake::free().with_serve(other);
    let (_, _, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_OK);
    assert_eq!(
        fake.serve_state(),
        json!({"TCP": {
            "9000": {"TCPForward": "127.0.0.1:9000"},
            PORT: {"TCPForward": "127.0.0.1:8443"},
        }})
    );
}

#[test]
fn a_serve_that_comes_up_as_something_else_is_named_and_left_alone() {
    let mut fake = Fake::free();
    fake.bg.effect = Effect::Tls;
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_UNDONE);
    assert_eq!(
        stderr,
        "expose: after `tailscale serve` (exit 0), tailnet port 8443 serves a TCP forward to 127.0.0.1:8443, TLS terminated for node.example.com, not the forward this command asked for; left alone. Check `tailscale serve status`\n"
    );
    assert_eq!(fake.mutations(), vec![words(&SERVE)], "no removal");
}

#[test]
fn a_refused_serve_leaves_the_port_as_it_was() {
    let mut fake = Fake::free();
    fake.bg = Step {
        code: 1,
        err: "serve: listener failed\n".into(),
        effect: Effect::Nothing,
        ..Step::default()
    };
    let (stdout, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_REFUSED);
    assert_eq!(stdout, plan());
    assert_eq!(
        stderr,
        "expose: tailscale refused the serve (serve: listener failed); nothing changed: fix what tailscale reports, then rerun this command\n"
    );
    assert_eq!(fake.calls(), reads_then(&SERVE), "the state was read back");
    assert_eq!(fake.serve_state(), json!({}));
}

#[test]
fn a_permission_refusal_names_the_fix() {
    let mut fake = Fake::free();
    fake.bg = Step {
        code: 1,
        err: "Access denied: serve config denied\n".into(),
        effect: Effect::Nothing,
        ..Step::default()
    };
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_REFUSED);
    assert_eq!(
        stderr,
        format!(
            "expose: tailscale refused the change for this user (Access denied: serve config denied); nothing changed: {}\n",
            permission_fix()
        )
    );
}

#[test]
fn move_reports_no_creation_only_after_the_child_has_exited_and_status_is_known() {
    for (timed_out, unsettled, failure) in [
        (false, false, Some("permission_denied")),
        (true, false, Some("serve_not_enabled")),
        (true, true, None),
    ] {
        let mut host = Fake::free();
        host.exposure_unsettled = unsettled;
        host.bg = Step {
            code: 1,
            err: "Access denied: serve config denied".into(),
            out: "https://login.tailscale.com/f/serve?fixture".into(),
            effect: Effect::Nothing,
            timed_out,
        };
        let args = Args {
            action: Action::Tailscale,
            port: 8443,
            json: true,
            yes: true,
        };
        let mut report = Report::new(&args);
        report.include_move_failure = true;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let report = runtime.block_on(expose(&host, &args, report));
        let json: Value =
            serde_json::from_str(&String::from_utf16(&report.stdout).unwrap()).unwrap();
        assert_eq!(json["move_failure"].as_str(), failure);
    }
}

#[test]
fn a_permission_refusal_whose_forward_appeared_is_undone_not_called_unchanged() {
    let mut fake = Fake::free();
    fake.bg = Step {
        code: 1,
        err: "applied\nAccess denied: later step\n".into(),
        ..Step::default()
    };
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_UNDONE);
    assert!(
        stderr.contains("although the forward appeared; it was removed again"),
        "{stderr}"
    );
    assert_eq!(fake.serve_state(), json!({}));
    // off: a refusal whose forward is gone anyway is the removal.
    let mut fake = Fake::ours();
    fake.off = Step {
        code: 1,
        err: "Access denied: later step\n".into(),
        ..Step::default()
    };
    let (stdout, _, code) = drive(&fake, &["off", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_OK);
    assert!(
        stdout.ends_with("no longer forwards to this daemon\n"),
        "{stdout}"
    );
}

#[test]
fn an_interrupted_serve_says_so_rather_than_refused() {
    let mut fake = Fake::free();
    fake.interrupt_during_change = true;
    fake.bg = Step {
        code: -2,
        effect: Effect::Nothing,
        ..Step::default()
    };
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_INTERRUPTED);
    assert!(
        stderr.ends_with("expose: `tailscale serve` was interrupted (exit -2); nothing changed\n"),
        "{stderr}"
    );
}

#[test]
fn an_undo_never_calls_a_forward_still_in_place_someone_elses() {
    // The serve failed although its forward appeared, the undo's removal
    // did nothing, and Funnel was switched on meanwhile: the forward is
    // still this run's, so the run says so and names the command.
    let mut fake = Fake::free();
    fake.bg.code = 1;
    fake.off.effect = Effect::Nothing;
    fake.meanwhile_off = Some(json!({"TCP": {PORT: {"TCPForward": "127.0.0.1:8443"}},
                                     "AllowFunnel": {"n.example.com:8443": true}}));
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_UNDONE);
    assert!(
        stderr.contains("and removing it again did not finish")
            && stderr.contains("run `tailscale serve --tcp=8443 off` yourself"),
        "{stderr}"
    );
}

#[test]
fn the_goal_met_after_the_question_reports_no_command_in_json() {
    let mut fake = Fake::ours().at_terminal(Answer::Yes);
    fake.meanwhile = Some(json!({}));
    let (stdout, _, code) = drive(&fake, &["off", "--port", PORT, "--json"]);
    assert_eq!(code, EXIT_OK);
    let report: Value = serde_json::from_str(&stdout).expect("one JSON report");
    assert_eq!(report["state"], "not_exposed");
    assert_eq!(report["command"], Value::Null, "nothing ran");
}

#[test]
fn an_undo_that_leaves_something_else_on_the_port_names_it_and_leaves_it() {
    let mut fake = Fake::free();
    fake.bg.code = 1;
    let theirs = json!({"TCP": {PORT: {"HTTPS": true}}});
    fake.meanwhile_off = Some(theirs.clone());
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_UNDONE);
    assert_eq!(
        stderr,
        "expose: `tailscale serve` failed (exit 1) although the forward appeared; after removing it, tailnet port 8443 serves HTTPS, which this command did not create; left alone. Check `tailscale serve status`\n"
    );
    assert_eq!(fake.serve_state(), theirs);
}

#[test]
fn a_link_a_wedged_serve_wrote_to_stderr_is_named() {
    let proc = Proc {
        code: 124,
        err: "timed out after 30s".into(),
        timed_out: true,
        err_before: "To enable, visit:\n  https://login.example.com/f/serve?node=n2\n".into(),
        ..Proc::default()
    };
    assert_eq!(
        printed(&proc).as_deref(),
        Some("https://login.example.com/f/serve?node=n2")
    );
    let quiet = Proc {
        err_before: "\n waiting \n".into(),
        ..proc
    };
    assert_eq!(printed(&quiet).as_deref(), Some("waiting"));
}

#[test]
fn a_serve_that_fails_after_its_forward_appeared_is_undone_and_checked() {
    let mut fake = Fake::free();
    fake.bg = Step {
        code: 1,
        err: "partial failure\n".into(),
        ..Step::default()
    };
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_UNDONE);
    assert_eq!(
        stderr,
        "expose: `tailscale serve` failed (partial failure) although the forward appeared; it was removed again, so nothing changed\n"
    );
    assert_eq!(fake.mutations(), vec![words(&SERVE), words(&OFF)]);
    assert_eq!(
        fake.calls().last(),
        Some(&words(&["serve", "status", "--json"]))
    );
    assert_eq!(fake.serve_state(), json!({}));
}

#[test]
fn an_undo_that_does_not_take_says_how_to_finish() {
    let mut fake = Fake::free();
    fake.bg.code = 1;
    fake.off = Step {
        code: 1,
        err: "off failed\n".into(),
        effect: Effect::Nothing,
        ..Step::default()
    };
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_UNDONE);
    assert_eq!(
        stderr,
        "expose: `tailscale serve` failed (exit 1) although the forward appeared, and removing it again did not finish (off failed; port 8443 still forwards to 127.0.0.1:8443): run `tailscale serve --tcp=8443 off` yourself\n"
    );
}

#[test]
fn a_timed_out_serve_names_what_it_asked_for_and_changes_nothing() {
    let mut fake = Fake::free();
    fake.bg = Step {
        code: 124,
        out: "Serve is not enabled on your tailnet.\nTo enable, visit:\n\n         https://login.example.com/f/serve?node=n1\n".into(),
        err: "timed out after 30s".into(),
        timed_out: true,
        effect: Effect::Nothing,
    };
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_REFUSED);
    assert_eq!(
        stderr,
        "expose: `tailscale serve` did not finish within 30s (https://login.example.com/f/serve?node=n1); nothing changed. Open that link to enable Serve for this tailnet, then rerun `pseudolife-mcp expose tailscale --port 8443`\n"
    );
    assert_eq!(fake.calls(), reads_then(&SERVE));
    assert_eq!(fake.serve_state(), json!({}));
}

#[test]
fn a_timed_out_serve_whose_forward_appeared_is_undone() {
    let mut fake = Fake::free();
    fake.bg = Step {
        code: 124,
        err: "timed out after 30s".into(),
        timed_out: true,
        ..Step::default()
    };
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_UNDONE);
    assert!(
        stderr.contains(
            "failed (timed out after 30s) although the forward appeared; it was removed again"
        ),
        "{stderr}"
    );
    assert_eq!(fake.serve_state(), json!({}));
}

#[test]
fn verification_catches_a_serve_that_did_not_take() {
    let mut fake = Fake::free();
    fake.bg.effect = Effect::Nothing;
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_UNDONE);
    assert_eq!(
        stderr,
        "expose: the serve did not take (`tailscale serve` exited 0, but the serve status does not show the forward); nothing changed. Check `tailscale serve status`, then rerun\n"
    );
    assert_eq!(fake.mutations(), vec![words(&SERVE)]);
}

#[test]
fn a_result_that_cannot_be_read_back_says_where_to_look() {
    let mut fake = Fake::free();
    fake.unreadable_after_change = true;
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_UNDONE);
    assert_eq!(
        stderr,
        "expose: the serve command ran (exit 0) but could not verify its result: could not read `tailscale serve status --json` (connection refused). Check `tailscale serve status`; if port 8443 forwards to 127.0.0.1:8443 and you do not want it, run `pseudolife-mcp expose off --port 8443`\n"
    );
}

#[test]
fn an_interrupt_during_the_change_still_reports_the_state_read_back() {
    let mut fake = Fake::free();
    fake.interrupt_during_change = true;
    let (stdout, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_OK);
    assert!(
        stderr.starts_with(
            "interrupted during the change: what follows is the serve state tailscale reports now\n"
        ),
        "{stderr}"
    );
    assert!(stdout.contains("exposed: clients use"));
}

// ── off ──────────────────────────────────────────────────────────────────────

#[test]
fn off_yes_removes_only_this_daemons_forward_then_verifies() {
    let mixed = json!({"TCP": {
        PORT: {"TCPForward": "127.0.0.1:8443"},
        "443": {"HTTPS": true},
        "9000": {"TCPForward": "127.0.0.1:9000"},
    }, "Web": {"n:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}});
    let fake = Fake::free().with_serve(mixed);
    let (stdout, stderr, code) = drive(&fake, &["off", "--port", PORT, "--yes"]);
    assert_eq!((code, stderr.as_str()), (EXIT_OK, ""));
    assert_eq!(
        fake.calls(),
        vec![
            words(&["status", "--json"]),
            words(&["serve", "status", "--json"]),
            words(&OFF),
            words(&["serve", "status", "--json"]),
        ]
    );
    assert_eq!(
        stdout,
        "plan:\n  run      tailscale serve --tcp=8443 off\nremoved: tailnet port 8443 no longer forwards to this daemon\n"
    );
    assert_eq!(
        fake.serve_state(),
        json!({"TCP": {"443": {"HTTPS": true}, "9000": {"TCPForward": "127.0.0.1:9000"}},
               "Web": {"n:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}})
    );
}

#[test]
fn off_on_a_host_that_is_not_exposed_changes_nothing_in_one_line() {
    let fake = Fake::free();
    let (stdout, stderr, code) = drive(&fake, &["off", "--port", PORT, "--yes"]);
    assert_eq!(
        (stdout.as_str(), stderr.as_str(), code),
        (
            "not exposed: nothing serves tailnet port 8443\n",
            "",
            EXIT_OK
        )
    );
    assert!(fake.mutations().is_empty());
}

#[test]
fn off_leaves_a_foreign_serve_on_the_port_alone() {
    let foreign = json!({"TCP": {PORT: {"TCPForward": "127.0.0.1:1"}}});
    let fake = Fake::free().with_serve(foreign.clone());
    let (_, stderr, code) = drive(&fake, &["off", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_REFUSED);
    assert_eq!(
        stderr,
        "expose: tailnet port 8443 serves a TCP forward to 127.0.0.1:1, not this daemon's forward to 127.0.0.1:8443; left alone\n"
    );
    assert!(fake.mutations().is_empty());
    assert_eq!(fake.serve_state(), foreign);
}

#[test]
fn off_at_a_terminal_asks_and_a_no_changes_nothing() {
    let fake = Fake::ours().at_terminal(Answer::No);
    let (_, stderr, code) = drive(&fake, &["off", "--port", PORT]);
    assert_eq!(
        (code, stderr),
        (EXIT_USAGE, format!("expose: {NOT_CONFIRMED}\n"))
    );
    assert_eq!(
        fake.asked.borrow().as_slice(),
        ["remove the tailnet forward on port 8443? [y/N] ".to_owned()]
    );
    assert!(fake.mutations().is_empty());
    let fake = Fake::ours().at_terminal(Answer::Yes);
    assert_eq!(drive(&fake, &["off", "--port", PORT]).2, EXIT_OK);
    assert_eq!(fake.serve_state(), json!({}));
}

#[test]
fn off_that_does_not_remove_the_forward_says_so() {
    for step in [
        Step {
            effect: Effect::Nothing,
            ..Step::default()
        },
        Step {
            code: 124,
            err: "timed out after 30s".into(),
            timed_out: true,
            effect: Effect::Nothing,
            ..Step::default()
        },
    ] {
        let mut fake = Fake::ours();
        fake.off = step.clone();
        let (_, stderr, code) = drive(&fake, &["off", "--port", PORT, "--yes"]);
        assert_eq!(code, EXIT_REFUSED, "{step:?}");
        let why = if step.timed_out {
            "timed out after 30s"
        } else {
            "exit 0"
        };
        assert_eq!(
            stderr,
            format!(
                "expose: tailscale did not remove the forward ({why}); it is still in place, so nothing changed: check `tailscale serve status`, then run the plan's `tailscale serve` command yourself\n"
            )
        );
        assert_eq!(
            fake.serve_state(),
            json!({"TCP": {PORT: {"TCPForward": "127.0.0.1:8443"}}})
        );
    }
}

#[test]
fn off_never_claims_a_removal_that_funnel_on_the_port_hides() {
    // The removal did nothing, and Funnel was switched on meanwhile: the
    // port reads as foreign, but this daemon's forward is still there.
    let mut fake = Fake::ours();
    fake.off.effect = Effect::Nothing;
    let still = json!({"TCP": {PORT: {"TCPForward": "127.0.0.1:8443"}},
                       "AllowFunnel": {"n.example.com:8443": true}});
    fake.meanwhile_off = Some(still.clone());
    let (stdout, stderr, code) = drive(&fake, &["off", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_REFUSED, "{stdout}{stderr}");
    assert!(!stdout.contains("removed"), "{stdout}");
    assert!(
        stderr.starts_with("expose: tailscale did not remove the forward (exit 0)"),
        "{stderr}"
    );
    assert_eq!(fake.serve_state(), still);
}

#[test]
fn off_reports_the_removal_and_names_what_serves_the_port_after_it() {
    let mut fake = Fake::ours();
    fake.meanwhile_off = Some(json!({"TCP": {PORT: {"HTTPS": true}}}));
    let (stdout, stderr, code) = drive(&fake, &["off", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_OK);
    assert!(
        stdout.ends_with("removed: this daemon's forward on tailnet port 8443\n"),
        "{stdout}"
    );
    assert_eq!(
        stderr,
        "tailnet port 8443 now serves HTTPS, which this command did not create; left alone\n"
    );
}

#[test]
fn off_that_fails_but_removed_the_forward_reports_the_verified_removal() {
    let mut fake = Fake::ours();
    fake.off.code = 1;
    let (stdout, _, code) = drive(&fake, &["off", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_OK);
    assert!(stdout.ends_with("removed: tailnet port 8443 no longer forwards to this daemon\n"));
}

#[test]
fn off_whose_result_cannot_be_read_back_says_where_to_look() {
    let mut fake = Fake::ours();
    fake.unreadable_after_change = true;
    let (_, stderr, code) = drive(&fake, &["off", "--port", PORT, "--yes", "--json"]);
    assert_eq!((code, stderr.as_str()), (EXIT_UNDONE, ""));
    let fake2 = {
        let mut fake = Fake::ours();
        fake.unreadable_after_change = true;
        fake
    };
    let (_, stderr, _) = drive(&fake2, &["off", "--port", PORT, "--yes"]);
    assert_eq!(
        stderr,
        "expose: the removal ran (exit 0) but could not verify its result: could not read `tailscale serve status --json` (connection refused). Check `tailscale serve status`; if port 8443 still forwards to 127.0.0.1:8443, run `tailscale serve --tcp=8443 off` yourself\n"
    );
}

#[test]
fn off_permission_refusal_names_the_fix() {
    let mut fake = Fake::ours();
    fake.off = Step {
        code: 1,
        err: "sudo: run with --operator to allow this\n".into(),
        effect: Effect::Nothing,
        ..Step::default()
    };
    let (_, stderr, code) = drive(&fake, &["off", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_REFUSED);
    assert!(
        stderr.ends_with(&format!("nothing changed: {}\n", permission_fix())),
        "{stderr}"
    );
}

#[test]
fn a_permission_refusal_names_the_least_privilege_fix_and_never_root() {
    let mut fake = Fake::free();
    fake.bg = Step {
        code: 1,
        err: "Access denied: serve config denied\n".into(),
        effect: Effect::Nothing,
        ..Step::default()
    };
    let (_, stderr, code) = drive(&fake, &["tailscale", "--port", PORT, "--yes"]);
    assert_eq!(code, EXIT_REFUSED);
    // Forwarding a port needs no root and no administrator shell, so the
    // refusal never sends the user to one.
    let lowered = stderr.to_lowercase();
    for word in ["as root", "elevated", "administrator"] {
        assert!(!lowered.contains(word), "{word}: {stderr}");
    }
    if cfg!(windows) {
        assert!(
            stderr.contains("owns this device's Tailscale configuration"),
            "{stderr}"
        );
    } else if cfg!(target_os = "macos") {
        assert!(
            stderr.contains("logged in to the Tailscale app"),
            "{stderr}"
        );
    } else {
        assert!(
            stderr.contains("operator once with `sudo tailscale set --operator=$USER`"),
            "{stderr}"
        );
    }
}

#[test]
fn the_permission_pattern_needs_its_exact_signal() {
    let refused = |err: &str| {
        permission_refused(&Proc {
            code: 1,
            err: err.into(),
            ..Proc::default()
        })
    };
    assert!(refused("ACCESS DENIED: nope"));
    assert!(refused("hint: use --operator"));
    assert!(refused("you must be root"));
    assert!(!refused("the operator is busy"));
    assert!(!refused("--operatorx"));
    assert!(!refused("listener failed"));
    assert!(!permission_refused(&Proc {
        code: 0,
        err: "access denied".into(),
        ..Proc::default()
    }));
}

// ── arguments and the report ─────────────────────────────────────────────────

#[test]
fn help_and_usage_errors_are_answered_at_any_width() {
    assert_eq!(answer(&["-h"]).stdout, units(args::MAIN_HELP));
    assert_eq!(answer(&["tailscale", "-h"]).code, EXIT_OK);
    assert_eq!(answer(&["bogus"]).code, EXIT_USAGE);
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
fn unicode_arguments_are_native_in_every_locale() {
    let fake = Fake::free();
    let out = decide(&argv(&["café"]), &fake);
    assert_eq!(out.code, EXIT_USAGE);
    assert!(fake.calls.borrow().is_empty());
    assert_eq!(decide(&argv(&["bogus"]), &Fake::free()).code, EXIT_USAGE);
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
    let out = report.fail_text(EXIT_USAGE, NO_TERMINAL);
    let text = String::from_utf16(&out.stdout).unwrap();
    assert_eq!(
        text,
        format!(
            "{{\n  \"action\": \"off\",\n  \"port\": 8443,\n  \"tailscale\": \"C:\\\\x\\\\tailscale.CMD\",\n  \"state\": null,\n  \"url\": null,\n  \"detail\": null,\n  \"command\": [\n    \"tailscale\",\n    \"serve\"\n  ],\n  \"daemon\": null,\n  \"probe\": null,\n  \"warnings\": [],\n  \"notes\": [],\n  \"error\": \"{NO_TERMINAL}\",\n  \"exit\": 2\n}}\n"
        )
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

#[test]
fn noncanonical_usage_never_reaches_tailscale() {
    for items in [
        vec!["status", "--po", "80"],
        vec!["tailscale", "--port", "-1"],
        vec!["off", "--port", "8_0"],
        vec!["--json", "status"],
    ] {
        let fake = Fake::free();
        let out = decide(&argv(&items), &fake);
        assert_eq!(out.code, EXIT_USAGE);
        assert!(fake.calls.borrow().is_empty());
        assert!(!fake.changed.get());
    }
}

#[cfg(unix)]
#[test]
fn undecodable_arguments_refuse_natively() {
    use std::os::unix::ffi::OsStringExt;
    assert_eq!(
        run(vec![OsString::from_vec(vec![0xff])]),
        Some(ExitCode::from(EXIT_USAGE))
    );
}

#[cfg(windows)]
#[test]
fn undecodable_arguments_refuse_natively() {
    use std::os::windows::ffi::OsStringExt;
    assert_eq!(
        run(vec![OsString::from_wide(&[0xd800])]),
        Some(ExitCode::from(EXIT_USAGE))
    );
}
