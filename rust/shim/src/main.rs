#![forbid(unsafe_code)]
mod argv;
use std::process::ExitCode;
fn main() -> ExitCode {
    // Episode hooks ignore every tail argument, including opaque OS strings.
    let first = std::env::args_os().nth(1);
    if let Some(code) = run_episode(first.as_deref().and_then(std::ffi::OsStr::to_str)) {
        return code;
    }
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    if let Some(mode) = arguments.first().filter(|mode| mode.to_str().is_none()) {
        pseudolife_stdio::stderrln!(
            "unknown mode {}; see: pseudolife-mcp --help",
            argv::python_repr(mode)
        );
        return ExitCode::from(2);
    }
    if arguments.first().is_some_and(|mode| mode == "wait-mail") {
        return ExitCode::from(pseudolife_stdio::cli::wait_mail::run(
            arguments.into_iter().skip(1).collect(),
        ));
    }
    if arguments.first().and_then(|mode| mode.to_str()) == Some("lease") {
        let Some(lease_arguments) = arguments
            .iter()
            .skip(1)
            .map(|argument| argument.to_str().map(str::to_owned))
            .collect::<Option<Vec<_>>>()
        else {
            pseudolife_stdio::stderrln!("lease: arguments must be valid Unicode");
            return ExitCode::from(2);
        };
        std::process::exit(run_lease(&lease_arguments));
    }
    if arguments.first().and_then(|mode| mode.to_str()) == Some("serve") {
        return run_sent();
    }
    if let Some(code) =
        pseudolife_stdio::cli::dispatch(arguments.first().and_then(|mode| mode.to_str()))
    {
        return code;
    }
    let channel = match arguments.as_slice() {
        [] => false,
        [mode] if mode.to_str() == Some("shim") => false,
        [mode] if mode.to_str() == Some("channel") => true,
        _ => {
            pseudolife_stdio::stderrln!("usage: pseudolife-stdio [shim|channel]");
            return ExitCode::FAILURE;
        }
    };
    serve_proxy(channel)
}

#[tokio::main(flavor = "current_thread")]
async fn run_episode(mode: Option<&str>) -> Option<ExitCode> {
    pseudolife_stdio::cli::dispatch_episode(mode).await
}

#[tokio::main(flavor = "current_thread")]
async fn run_lease(arguments: &[String]) -> i32 {
    pseudolife_stdio::cli::lease::main(arguments).await
}

#[tokio::main(flavor = "current_thread")]
async fn run_sent() -> ExitCode {
    pseudolife_stdio::sent_serve::run().await
}

#[tokio::main(flavor = "current_thread")]
async fn serve_proxy(channel: bool) -> ExitCode {
    let proxy = match pseudolife_stdio::Proxy::attach_mode(channel).await {
        Ok(proxy) => proxy,
        Err(error) => {
            pseudolife_stdio::stderrln!("{error}");
            return ExitCode::from(error.code.try_into().unwrap_or(1));
        }
    };
    let runtime = proxy.runtime();
    let board = proxy.board();
    let ownership = proxy.ownership();
    let updates = runtime.clone();
    let result = pseudolife_stdio::serve_after_first_frame(proxy, ownership, move || {
        updates.client_updates_after_first_frame();
    })
    .await;
    board.close().await;
    runtime.close_episode().await;
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            pseudolife_stdio::stderrln!("pseudolife-mcp: {error}");
            ExitCode::FAILURE
        }
    }
}
