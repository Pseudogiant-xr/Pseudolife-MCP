//! Native process leaves at Python lease_cli.py pin 165f4125.
mod args;
mod board;
mod hold;
mod json;
mod lock;
pub mod operator;
mod run;
mod view;

fn whitespace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

fn repr(text: &str) -> String {
    super::mode_repr(text)
}
fn write(text: &str, stderr: bool) -> std::io::Result<()> {
    use std::io::Write;
    let bytes = super::text_bytes(text);
    if stderr {
        std::io::stderr().lock().write_all(&bytes)
    } else {
        std::io::stdout().lock().write_all(&bytes)
    }
}
static OUTPUT_FAILED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
fn output_error(_error: &std::io::Error) -> String {
    "stdout write failed".into()
}
fn flush_failure(error: &std::io::Error) {
    let _ = write(&format!("lease: {}\n", output_error(error)), true);
}
fn out(text: &str, stderr: bool) {
    if let Err(error) = write(text, stderr) {
        OUTPUT_FAILED.store(true, std::sync::atomic::Ordering::Relaxed);
        if !stderr {
            flush_failure(&error);
        }
    }
}
fn say(text: &str) {
    out(&(text.to_owned() + "\n"), true);
}
pub async fn main(arguments: &[String]) -> i32 {
    let args = match args::parse(arguments) {
        Ok(args) => args,
        Err(code) => {
            return if OUTPUT_FAILED.load(std::sync::atomic::Ordering::Relaxed) {
                120
            } else {
                code
            };
        }
    };
    let code = match args.action.as_str() {
        "check" => view::check(&args).await,
        "list" => view::list(&args).await,
        "hold" => hold::hold(args).await,
        "break" => operator::command(&args, arguments).await,
        "delegate" => {
            say(&format!(
                "pseudolife-stdio: lease action {} is deferred in this candidate",
                repr(&args.action)
            ));
            1
        }
        _ => run::run(args).await,
    };
    if OUTPUT_FAILED.load(std::sync::atomic::Ordering::Relaxed) {
        120
    } else {
        code
    }
}
