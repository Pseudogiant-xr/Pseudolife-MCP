//! Audit argparse help text, captured from the Python contract.
pub(super) fn text(action: &str) -> &'static str {
    match action {
        "" => {
            r###"usage: pseudolife-mcp board-audit [-h] {export,verify,redact,stats} ...

Export, verify, redact or compute stats on the agent board's audit log
(operator-only). Reads PSEUDOLIFE_MCP_DATABASE_URL, or the lite tier's bank,
else runs inside the pseudolife-mcp-daemon container when it runs here. An
export holds message bodies: keep it private.

positional arguments:
  {export,verify,redact,stats}
    export              write events as JSON lines, oldest first
    verify              check the hash chain and print its head
    redact              remove one message body (sent from schema v46 on) and
                        log why
    stats               one JSON object of coordination telemetry for a window
                        (default the last 24 h): mail latency, wake precision,
                        park outcomes, sends per session-hour, suite lock
                        waits; counts, seconds and names only

options:
  -h, --help            show this help message and exit
"###
        }
        "export" => {
            r###"usage: pseudolife-mcp board-audit export [-h] [--project PROJECT]
                                         [--task TASK] [--agent AGENT]
                                         [--since SINCE] [--until UNTIL]
                                         [--out OUT]

options:
  -h, --help         show this help message and exit
  --project PROJECT  only events in this project
  --task TASK        only events in this task
  --agent AGENT      only events by this agent or to its mailbox (a full id,
                     or a unique prefix of 8 or more characters)
  --since SINCE      epoch seconds or ISO 8601, inclusive
  --until UNTIL      epoch seconds or ISO 8601, exclusive
  --out OUT          a NEW file to write instead of stdout
"###
        }
        "verify" => {
            r###"usage: pseudolife-mcp board-audit verify [-h] [--expect-head SEQ:HASH]
                                         [--input INPUT]

options:
  -h, --help            show this help message and exit
  --expect-head SEQ:HASH
                        a head recorded earlier; fails if it was dropped or
                        rewritten
  --input INPUT         verify an unfiltered export file instead of the bank
"###
        }
        "redact" => {
            r###"usage: pseudolife-mcp board-audit redact [-h] --message-id MESSAGE_ID --reason
                                         REASON

options:
  -h, --help            show this help message and exit
  --message-id MESSAGE_ID
                        the message whose body to remove (a full id, or a
                        unique prefix of 8 or more characters)
  --reason REASON       why, kept in the log for good (at most 240 characters;
                        never repeat the secret)
"###
        }
        "stats" => {
            r###"usage: pseudolife-mcp board-audit stats [-h] [--since SINCE] [--until UNTIL]
                                        [--input INPUT]
                                        [--durations DURATIONS] [--out OUT]
                                        [--append APPEND]

options:
  -h, --help            show this help message and exit
  --since SINCE         epoch seconds or ISO 8601, inclusive (default: 24 h
                        before --until)
  --until UNTIL         epoch seconds or ISO 8601, exclusive (default: now)
  --input INPUT         read events from an export file instead of the bank
                        (wake rows are not exported, so wake precision is
                        empty)
  --durations DURATIONS
                        the suite lock's durations file (default: full-
                        suite.durations.jsonl in the lock directory)
  --out OUT             a NEW file to write the object to instead of stdout
  --append APPEND       a JSON lines file to append the object to (created if
                        missing)
"###
        }
        _ => unreachable!("known audit action"),
    }
}
