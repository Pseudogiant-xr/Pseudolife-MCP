**MCP-REFUSAL-CLEANUP** (narrowed 2026-10-20): every refusal, REST, hook
or MCP, is answered at once, as uvicorn answers it. The rest of the body is
then discarded, at most 8 MiB more (twice the largest admitted body) within
250 ms; a body read to its end keeps the connection, as Python's does. An
unread `Expect: 100-continue` body (never polled, so Hyper sends no interim
100) or a declared `Content-Length` past that budget is not read, and that
answer carries `Connection: close`, so no client pools a connection about to
close; Python keeps both open and discards without a limit. A connection
that closes (those two, a budget or deadline cut, a failed read) goes
through the listener's lingering close (`src/linger.rs`): FIN after the
answer, then discard until the client closes, 250 ms pass with nothing
arriving, or 2 s pass. Before this the daemon closed with request bytes
unread, its stack sent RST, and Windows clients discarded the answer they
had already received: a read-after-send probe on Windows 11 lost 34 of 2000
refusals idle and 117 of 2000 under 15 CPU burners, against 0 of 3800 for
Python. The strict MCP corpus keeps two `body_limit` differences (indices 4
and 5, GETs declaring 9999999 bytes: the added `Connection: close` on the
413); `mcp_ci.py` declares exactly those. Indices 0, 2 and 3, which used to
differ the same way, now match.
