//! What `connect` needs from the unattended-update schedule: the schedules
//! installed here, read back beside the code that writes them, and their
//! re-creation for a new daemon URL through `--schedule` itself.
use super::*;
use crate::cli::update::Options;

/// One installed schedule, as `connect` reports and re-creates it.
pub(crate) struct Installed {
    /// The client schedule (what `--schedule` writes on a shim runtime with
    /// no daemon container) rather than the daemon's.
    pub client: bool,
    /// Where it lives, as a report names it.
    pub place: String,
    /// Its daily time, `HH:MM`, when it can be read.
    pub time: Option<String>,
    /// The daemon URL its command names.
    pub url: Option<String>,
    /// It is exactly what `--schedule` writes here for `url` at `time`:
    /// the command, and (systemd) the unit, its environment and its timer,
    /// enabled; (Task Scheduler) one daily trigger, enabled, interactive.
    pub shaped: bool,
    /// Why that cannot be told (no launcher to compare with, a description
    /// that cannot be read), when it cannot.
    pub why: Option<String>,
    /// The one command that removes exactly this schedule.
    pub remove: String,
}

/// The daemon and client schedules installed on this machine.
pub(crate) fn installed(names: &Names) -> Vec<Installed> {
    installed_with(names, &|url: &str| command(url))
}

fn installed_with(names: &Names, expected: &dyn Fn(&str) -> Result<Vec<String>>) -> Vec<Installed> {
    let mut found = Vec::new();
    for client in [false, true] {
        let item = if cfg!(windows) {
            let task = if client {
                names.client_task
            } else {
                names.task
            };
            let (code, xml) = seams::run_cli(
                &[
                    "schtasks".into(),
                    "/Query".into(),
                    "/TN".into(),
                    task.into(),
                    "/XML".into(),
                ],
                None,
                30,
                false,
            );
            (code == 0).then(|| {
                windows_parse(task, client, &xml, &|url| schedule_windows(&expected(url)?))
            })
        } else if cfg!(target_os = "linux") {
            let name = if client {
                names.client_unit
            } else {
                names.unit
            };
            let dir = systemd_dir();
            let unit = dir.join(format!("{name}.service"));
            fs::read_to_string(&unit).ok().map(|service| {
                let timer = fs::read_to_string(unit.with_extension("timer")).unwrap_or_default();
                let enabled = seams::run_cli(
                    &[
                        "systemctl".into(),
                        "--user".into(),
                        "is-enabled".into(),
                        "--quiet".into(),
                        format!("{name}.timer"),
                    ],
                    None,
                    30,
                    false,
                )
                .0 == 0;
                systemd_parse(&dir, name, client, &service, &timer, enabled, &|url| {
                    service_body(name, &expected(url)?, &environment_lines(client, false)?)
                })
            })
        } else {
            None
        };
        found.extend(item);
    }
    found
}

fn between<'a>(text: &'a str, open: &str, close: &str) -> Option<&'a str> {
    let start = text.find(open)? + open.len();
    let end = text[start..].find(close)?;
    Some(&text[start..start + end])
}

fn xml_unescape(text: &str) -> String {
    text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

/// `HH:MM` from the start of `text`, when it is one.
fn clock(text: &str) -> Option<String> {
    let time = text.get(..5)?;
    let bytes = time.as_bytes();
    (bytes[2] == b':'
        && [0, 1, 3, 4].iter().all(|&i| bytes[i].is_ascii_digit())
        && at(time).is_ok())
    .then(|| time.to_owned())
}

/// The value after `flag` in a command line that quotes each argument with
/// `quote`, as `--schedule` writes it.
fn flag_value(text: &str, quote: char) -> Option<String> {
    let open = format!("{quote}--daemon-url{quote} {quote}");
    let start = text.find(&open)? + open.len();
    let end = text[start..].find(quote)?;
    Some(text[start..start + end].to_owned())
}

fn decode_base64(text: &str) -> Option<Vec<u8>> {
    let value = |c: u8| -> Option<u32> {
        Some(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32)
    };
    let bytes = text.trim().as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return None;
    }
    let mut out = Vec::new();
    let last = bytes.len() / 4;
    for (index, chunk) in bytes.chunks(4).enumerate() {
        let pad = chunk.iter().rev().take_while(|&&c| c == b'=').count();
        // At most two pad characters, and only at the very end.
        if pad > 2 || (pad > 0 && index + 1 != last) {
            return None;
        }
        let mut n = 0u32;
        for &c in &chunk[..4 - pad] {
            n = (n << 6) | value(c)?;
        }
        n <<= 6 * pad as u32;
        let decoded = [(n >> 16) as u8, (n >> 8) as u8, n as u8];
        out.extend_from_slice(&decoded[..3 - pad]);
    }
    Some(out)
}

/// The daemon URL a task's arguments name: plain, or inside the encoded
/// wrapper `--schedule` writes when layout overrides are set.
fn windows_url(arguments: &str) -> Option<String> {
    if let Some(encoded) = arguments
        .split_once("-EncodedCommand ")
        .map(|(_, encoded)| encoded)
    {
        let bytes = decode_base64(encoded)?;
        let units: Vec<u16> = bytes
            .chunks_exact(2)
            .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
            .collect();
        return flag_value(&String::from_utf16(&units).ok()?, '\'');
    }
    flag_value(arguments, '"')
}

/// A scheduled task's `/XML`, as `--schedule` registers it: one action
/// exactly as it registers it for the URL it names (`expected` gives that),
/// one daily calendar trigger, nothing disabled, an interactive logon.
fn windows_parse(
    task: &str,
    client: bool,
    xml: &str,
    expected: &dyn Fn(&str) -> Result<(String, String)>,
) -> Installed {
    let executable = between(xml, "<Command>", "</Command>").map(xml_unescape);
    let arguments = between(xml, "<Arguments>", "</Arguments>").map(xml_unescape);
    let time = between(xml, "<StartBoundary>", "</StartBoundary>")
        .and_then(|start| start.split_once('T'))
        .and_then(|(_, time)| clock(time));
    let url = arguments.as_deref().and_then(windows_url);
    let triggers = between(xml, "<Triggers>", "</Triggers>").unwrap_or_default();
    let as_registered = xml.matches("<Exec>").count() == 1
        && triggers.matches("Trigger>").count() == 2
        && triggers.contains("<CalendarTrigger>")
        && triggers.contains("<DaysInterval>1</DaysInterval>")
        && !xml.contains("<Enabled>false</Enabled>")
        && xml.contains("<LogonType>InteractiveToken</LogonType>");
    let mut why = None;
    // schtasks writes its XML in the console code page; a path outside it
    // arrives as replacement characters and cannot be compared.
    if xml.contains('\u{fffd}') {
        why = Some("Task Scheduler's description of it holds characters connect cannot read, such as a path outside the console code page".to_owned());
    }
    let shaped = why.is_none()
        && as_registered
        && match (&executable, &arguments, &url) {
            (Some(executable), Some(arguments), Some(url)) => match expected(url) {
                Ok((want, args)) => &want == executable && &args == arguments,
                Err(error) => {
                    why = Some(error.message);
                    false
                }
            },
            _ => false,
        };
    Installed {
        client,
        place: format!("the scheduled task '{task}'"),
        time,
        url,
        shaped,
        why,
        remove: format!("schtasks /Delete /TN \"{task}\" /F"),
    }
}

/// A systemd unit and its timer, as `--schedule` installs them: the service
/// file exactly as it writes it for the URL it names (`expected` gives it),
/// the timer exactly as it writes it for the time it names, enabled.
fn systemd_parse(
    dir: &Path,
    name: &str,
    client: bool,
    service: &str,
    timer: &str,
    enabled: bool,
    expected: &dyn Fn(&str) -> Result<String>,
) -> Installed {
    let url = service
        .lines()
        .find_map(|line| line.strip_prefix("ExecStart="))
        .and_then(|exec| flag_value(exec, '"'));
    let time = timer
        .lines()
        .filter_map(|line| line.strip_prefix("OnCalendar="))
        .find_map(|calendar| calendar.split_whitespace().nth(1).and_then(clock));
    let timer_shaped = time
        .as_deref()
        .and_then(|time| at(time).ok())
        .is_some_and(|(hour, minute)| timer == timer_body(name, hour, minute));
    let mut why = None;
    let shaped = enabled
        && timer_shaped
        && match url.as_deref() {
            Some(url) => match expected(url) {
                Ok(want) => want == service,
                Err(error) => {
                    why = Some(error.message);
                    false
                }
            },
            None => false,
        };
    let file = |suffix: &str| format!("'{}'", dir.join(format!("{name}.{suffix}")).display());
    Installed {
        client,
        place: dir.join(format!("{name}.service")).display().to_string(),
        time,
        url,
        shaped,
        why,
        remove: format!(
            "systemctl --user disable --now {name}.timer && rm {} {} && systemctl --user daemon-reload",
            file("service"),
            file("timer")
        ),
    }
}

fn options(url: &str) -> Update {
    Update::new(Options {
        daemon_url: url.to_owned(),
        as_json: true,
        ..Options::default()
    })
}

/// Why `--schedule` here would not re-create a schedule: `other_kind` when
/// it would write the other kind of schedule instead.
pub(crate) struct Refusal {
    pub other_kind: bool,
    pub reason: String,
}

fn same_kind(update: &Update, client: bool) -> std::result::Result<(), Refusal> {
    let other = |reason: &str| Refusal {
        other_kind: true,
        reason: reason.to_owned(),
    };
    match client_kind(update) {
        Ok(here) if here == client => Ok(()),
        Ok(true) => Err(other(
            "this machine runs no daemon container, so `update --schedule` here writes the client schedule instead",
        )),
        Ok(false) => Err(other(
            "this machine runs the daemon container, so `update --schedule` here writes the daemon's schedule instead",
        )),
        Err(error) => Err(Refusal {
            other_kind: false,
            reason: error.message,
        }),
    }
}

/// Why `--schedule` here would not re-create the schedule of this kind for
/// `url` (a different kind, an unlocatable launcher, no bearer for the
/// daemon's run), checked before anything is written.
pub(crate) fn preflight(client: bool, url: &str) -> std::result::Result<(), Refusal> {
    let update = options(url);
    same_kind(&update, client)?;
    let refuse = |reason: String| Refusal {
        other_kind: false,
        reason,
    };
    command(url).map_err(|error| refuse(error.message))?;
    if !client && !daemon_bearer_resolves() {
        return Err(refuse(no_bearer_text(cfg!(windows))));
    }
    Ok(())
}

/// Re-create the schedule of this kind daily at `when` against `url`
/// through `--schedule`; the steps it reports, or why it refused.
pub(crate) fn reschedule(
    names: &Names,
    client: bool,
    when: &str,
    url: &str,
) -> std::result::Result<Vec<String>, String> {
    let mut update = options(url);
    update.o.schedule = Some(when.to_owned());
    same_kind(&update, client).map_err(|refusal| refusal.reason)?;
    schedule_named(&mut update, when, names).map_err(|error| error.message)?;
    Ok(update.report.steps)
}

#[cfg(test)]
mod tests {
    use super::*;

    const URL: &str = "http://127.0.0.1:8765";
    const NAME: &str = "pseudolife-update";

    fn argv(url: &str) -> Result<Vec<String>> {
        Ok(vec![
            r"C:\fixture\bin\pseudolife-mcp.exe".into(),
            "update".into(),
            "--unattended".into(),
            "--daemon-url".into(),
            url.into(),
        ])
    }

    /// A task's `/XML` as `--schedule` registers it, with `edit` applied.
    fn task_xml(command: &str, arguments: &str, edit: impl Fn(String) -> String) -> String {
        let escape = |text: &str| text.replace('&', "&amp;").replace('"', "&quot;");
        edit(format!(
            "<Task><Triggers><CalendarTrigger><StartBoundary>2026-11-14T03:07:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers><Principals><Principal id=\"Author\"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><Enabled>true</Enabled></Settings><Actions Context=\"Author\"><Exec><Command>{}</Command><Arguments>{}</Arguments></Exec></Actions></Task>",
            escape(command),
            escape(arguments)
        ))
    }

    fn windows_expected(url: &str) -> Result<(String, String)> {
        let argv = argv(url)?;
        let arguments = argv[1..]
            .iter()
            .map(|s| windows_quote(s))
            .collect::<Result<Vec<_>>>()?
            .join(" ");
        Ok((argv[0].clone(), arguments))
    }

    #[test]
    fn a_task_as_schedule_registers_it_reads_back_its_url_and_time() {
        let (command, arguments) = windows_expected(URL).unwrap();
        let found = windows_parse(
            "T",
            true,
            &task_xml(&command, &arguments, |x| x),
            &windows_expected,
        );
        assert!(found.shaped);
        assert!(found.client);
        assert!(found.why.is_none());
        assert_eq!(found.url.as_deref(), Some(URL));
        assert_eq!(found.time.as_deref(), Some("03:07"));
        assert_eq!(found.place, "the scheduled task 'T'");
        assert_eq!(found.remove, "schtasks /Delete /TN \"T\" /F");
    }

    #[test]
    fn a_task_changed_from_what_schedule_registers_is_not_shaped() {
        let (command, arguments) = windows_expected(URL).unwrap();
        let shaped = |xml: String| windows_parse("T", false, &xml, &windows_expected).shaped;
        // Near miss: unchanged, it is shaped.
        assert!(shaped(task_xml(&command, &arguments, |x| x)));
        // The same URL, with an argument added by hand.
        let edited = windows_parse(
            "T",
            false,
            &task_xml(
                &command,
                &format!("{arguments} \"--tag\" \"0.23.1\""),
                |x| x,
            ),
            &windows_expected,
        );
        assert_eq!(edited.url.as_deref(), Some(URL));
        assert!(!edited.shaped);
        assert!(!shaped(task_xml(
            r"C:\elsewhere\pseudolife-mcp.exe",
            &arguments,
            |x| x
        )));
        for (what, edit) in [
            (
                "a second action",
                Box::new(|x: String| {
                    x.replace("</Actions>", "<Exec><Command>x</Command></Exec></Actions>")
                }) as Box<dyn Fn(String) -> String>,
            ),
            (
                "disabled",
                Box::new(|x: String| {
                    x.replace("<Settings><Enabled>true", "<Settings><Enabled>false")
                }),
            ),
            (
                "weekly",
                Box::new(|x: String| {
                    x.replace(
                        "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>",
                        "<ScheduleByWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>",
                    )
                }),
            ),
            (
                "a second trigger",
                Box::new(|x: String| {
                    x.replace(
                        "</Triggers>",
                        "<LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>",
                    )
                }),
            ),
            (
                "run whether logged on or not",
                Box::new(|x: String| x.replace("InteractiveToken", "Password")),
            ),
        ] {
            assert!(!shaped(task_xml(&command, &arguments, edit)), "{what}");
        }
    }

    #[test]
    fn a_task_that_cannot_be_compared_says_why() {
        let (command, arguments) = windows_expected(URL).unwrap();
        let no_launcher = |_: &str| -> Result<(String, String)> {
            Err(UpdateError::usage("no verified stable native launcher"))
        };
        let found = windows_parse(
            "T",
            false,
            &task_xml(&command, &arguments, |x| x),
            &no_launcher,
        );
        assert!(!found.shaped);
        assert_eq!(
            found.why.as_deref(),
            Some("no verified stable native launcher")
        );
        let lossy = task_xml(&command.replace("fixture", "Zo\u{fffd}"), &arguments, |x| x);
        let found = windows_parse("T", false, &lossy, &windows_expected);
        assert!(!found.shaped);
        assert!(found.why.unwrap().contains("console code page"));
    }

    #[test]
    fn an_encoded_layout_wrapper_names_its_url() {
        let script = format!(
            "$env:XDG_DATA_HOME='C:\\x';& 'C:\\b.exe' 'update' '--unattended' '--daemon-url' '{URL}';exit $LASTEXITCODE"
        );
        let encoded = base64(
            &script
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
        );
        assert_eq!(
            windows_url(&format!(
                "-NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}"
            ))
            .as_deref(),
            Some(URL)
        );
        // Malformed padding (a hand-edited task) reads as no URL, never a panic.
        for text in ["====", "A===", "AB=C", "AB==AB==", "A"] {
            assert_eq!(decode_base64(text), None, "{text}");
        }
        assert_eq!(windows_url("-EncodedCommand ===="), None);
        for text in ["", "a", "ab", "abc", "abcd", "\u{e9}t\u{e9}"] {
            assert_eq!(
                decode_base64(&base64(text.as_bytes())).unwrap(),
                text.as_bytes()
            );
        }
    }

    #[test]
    fn a_unit_as_schedule_installs_it_reads_back_and_anything_else_is_not_shaped() {
        let environment = vec![format!(
            "Environment={}",
            unit_quote(
                "PSEUDOLIFE_MCP_TOKEN_FILE=/home/u/.pseudolife-mcp/notice.token",
                false
            )
            .unwrap()
        )];
        let service = service_body(NAME, &argv(URL).unwrap(), &environment).unwrap();
        let timer = timer_body(NAME, 22, 45);
        let expected = |url: &str| service_body(NAME, &argv(url)?, &environment);
        let dir = Path::new("/home/u/.config/systemd/user");
        let parse = |service: &str, timer: &str, enabled: bool| {
            systemd_parse(dir, NAME, false, service, timer, enabled, &expected)
        };
        let found = parse(&service, &timer, true);
        assert!(found.shaped);
        assert!(!found.client);
        assert_eq!(found.url.as_deref(), Some(URL));
        assert_eq!(found.time.as_deref(), Some("22:45"));
        assert_eq!(
            found.place,
            dir.join("pseudolife-update.service").display().to_string()
        );
        assert!(
            found
                .remove
                .starts_with("systemctl --user disable --now pseudolife-update.timer && rm ")
        );
        // Near misses: a flag added by hand, another bearer file, a second
        // ExecStart, a monthly calendar, no timer, a disabled timer.
        let edited = service.replace("--unattended\"", "--unattended\" \"--tag\" \"0.23.1\"");
        assert!(!parse(&edited, &timer, true).shaped);
        let other_bearer = service.replace("notice.token", "other.token");
        assert!(!parse(&other_bearer, &timer, true).shaped);
        let twice = format!("{service}ExecStart=/bin/true\n");
        assert!(!parse(&twice, &timer, true).shaped);
        let monthly = timer.replace("OnCalendar=*-*-* 22:45:00", "OnCalendar=*-*-01 22:45:00");
        let found = parse(&service, &monthly, true);
        assert!(!found.shaped);
        assert_eq!(found.time.as_deref(), Some("22:45"));
        assert_eq!(parse(&service, "", true).time, None);
        assert!(!parse(&service, "", true).shaped);
        assert!(!parse(&service, &timer, false).shaped);
    }

    #[test]
    fn a_time_must_be_a_clock_reading() {
        assert_eq!(clock("03:07:00").as_deref(), Some("03:07"));
        for text in ["3:07", "24:00", "03:60", "0a:07", "03-07", ""] {
            assert_eq!(clock(text), None, "{text}");
        }
    }
}

/// A real scheduler under a name unique to the run: registered as
/// `--schedule` writes it, read back, re-created against a new URL keeping
/// its time, then removed and checked gone. Run only on a dev machine.
#[cfg(all(test, any(windows, target_os = "linux")))]
mod real_tests {
    use super::*;

    #[test]
    #[ignore = "creates a real scheduled task or systemd user timer under a unique name; dev machines only"]
    fn a_real_schedule_reads_back_and_is_re_created_against_a_new_url() {
        let unique = format!("test-{}", uuid::Uuid::new_v4().simple());
        let task = format!("Pseudolife Client Update {unique}");
        let unit = format!("{CLIENT_UNIT}-{unique}");
        let names = Names {
            task: "unused",
            client_task: &task,
            unit: "unused",
            client_unit: &unit,
        };
        let binary = std::env::current_exe()
            .unwrap()
            .to_str()
            .unwrap()
            .to_owned();
        let argv = |url: &str| -> Result<Vec<String>> {
            Ok(vec![
                binary.clone(),
                "update".into(),
                "--unattended".into(),
                "--daemon-url".into(),
                url.into(),
            ])
        };
        let remove = || {
            if cfg!(windows) {
                seams::run_cli(
                    &[
                        "schtasks".into(),
                        "/Delete".into(),
                        "/F".into(),
                        "/TN".into(),
                        task.clone(),
                    ],
                    None,
                    30,
                    false,
                );
            } else {
                seams::run_cli(
                    &[
                        "systemctl".into(),
                        "--user".into(),
                        "disable".into(),
                        "--now".into(),
                        format!("{unit}.timer"),
                    ],
                    None,
                    30,
                    false,
                );
                for suffix in ["service", "timer"] {
                    let _ = fs::remove_file(systemd_dir().join(format!("{unit}.{suffix}")));
                }
                seams::run_cli(
                    &["systemctl".into(), "--user".into(), "daemon-reload".into()],
                    None,
                    30,
                    false,
                );
            }
        };
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut update = options("http://127.0.0.1:18765");
            write(
                &mut update,
                &names,
                true,
                4,
                9,
                &argv("http://127.0.0.1:18765").unwrap(),
            )
            .unwrap();
            let found: Vec<_> = installed_with(&names, &argv)
                .into_iter()
                .filter(|i| i.client)
                .collect();
            assert_eq!(found.len(), 1);
            assert!(found[0].shaped, "{} {:?}", found[0].place, found[0].why);
            assert_eq!(found[0].url.as_deref(), Some("http://127.0.0.1:18765"));
            assert_eq!(found[0].time.as_deref(), Some("04:09"));
            // Re-created against the new URL at the time read back.
            let time = found[0].time.clone().unwrap();
            let (hour, minute) = at(&time).unwrap();
            write(
                &mut update,
                &names,
                true,
                hour,
                minute,
                &argv("http://127.0.0.1:28765").unwrap(),
            )
            .unwrap();
            let again: Vec<_> = installed_with(&names, &argv)
                .into_iter()
                .filter(|i| i.client)
                .collect();
            assert_eq!(again.len(), 1);
            assert!(again[0].shaped);
            assert_eq!(again[0].url.as_deref(), Some("http://127.0.0.1:28765"));
            assert_eq!(again[0].time.as_deref(), Some("04:09"));
        }));
        remove();
        let gone = installed_with(&names, &argv).into_iter().all(|i| !i.client);
        assert!(
            gone,
            "the test schedule {task} / {unit} was not removed; remove it by hand"
        );
        if let Err(panic) = result {
            std::panic::resume_unwind(panic);
        }
    }
}
