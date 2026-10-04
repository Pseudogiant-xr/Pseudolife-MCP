#![forbid(unsafe_code)]
use std::process::ExitCode;
#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let arguments: Vec<_> = std::env::args().skip(1).collect();
    if let Some(code) = pseudolife_stdio::cli::dispatch(arguments.first().map(String::as_str)) {
        return code;
    }
    let channel = arguments.first().is_some_and(|mode| mode == "channel");
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
