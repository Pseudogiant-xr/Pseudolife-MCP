#![forbid(unsafe_code)]
use std::process::ExitCode;
#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let arguments: Vec<_> = std::env::args().skip(1).collect();
    let channel = match arguments.as_slice() {
        [] => false,
        [mode] if mode == "shim" => false,
        [mode] if mode == "channel" => true,
        _ => {
            pseudolife_stdio::stderrln!("usage: pseudolife-stdio [shim|channel]");
            return ExitCode::FAILURE;
        }
    };
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
    let result = pseudolife_stdio::serve(proxy, ownership).await;
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
