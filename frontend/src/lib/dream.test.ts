import { describe, expect, it } from "vitest";
import { describeDreamRun, stallNotice } from "./dream";
import { splitBackticks } from "./insight";

// Shapes from service_dream.dream_run: _held() returns no error and no
// skipped, only extractor_failed with the phase and the extractor's error.
describe("describeDreamRun", () => {
  it("reports a held dream as held, never as finished", () => {
    const o = describeDreamRun({
      pulled: 12,
      claims: 0,
      inserted: 0,
      extractor_failed: true,
      hold_phase: "extract",
      extractor_error: { reason: "auth_expired", error: "CLI login expired" },
    });
    expect(o.tone).toBe("danger");
    expect(o.message).toContain("held");
    expect(o.message).toContain("CLI login expired");
    expect(o.message).not.toContain("finished");
  });

  it("names the database for a write-phase hold", () => {
    const o = describeDreamRun({ pulled: 3, extractor_failed: true, hold_phase: "write" });
    expect(o.tone).toBe("danger");
    expect(o.message).toContain("database");
  });

  it("warns when every pulled memory was set aside", () => {
    const o = describeDreamRun({ pulled: 1, claims: 0, quarantined: 1 });
    expect(o.tone).toBe("warn");
    expect(o.message).not.toContain("finished");
  });

  it("summarises a real run, edges and lessons included", () => {
    const o = describeDreamRun({ pulled: 2, claims: 5, inserted: 3, superseded: 1, relations: 4, lessons: 1, extractor: "primary" });
    expect(o.tone).toBe("ok");
    expect(o.message).toBe(
      "The dream finished with the primary extractor: 2 memories pulled, 5 claims, 3 new, 1 superseded, 4 edges, 1 lesson.",
    );
  });

  it("keeps error, skipped and empty answers distinct", () => {
    expect(describeDreamRun({ error: "dream_in_progress" }).tone).toBe("danger");
    expect(describeDreamRun({ skipped: "already_running" }).message).toContain("already running");
    expect(describeDreamRun({ pulled: 0 }).message).toContain("Nothing was waiting");
  });
});

// GET /api/dream/status and /api/overview's dream block carry stall_notice:
// the daemon's rendered line while dreams are stalled, else null.
describe("stallNotice", () => {
  const NOTICE =
    "Pseudolife-MCP: dreams stalled since 2026-10-27 03:01+11:00 (login_expired): the Claude Code CLI is signed out on the machine running the Pseudolife daemon; sign it in there as svc: run `claude`, then `/login`.";

  it("shows the daemon's notice verbatim", () => {
    expect(stallNotice({ stall_notice: NOTICE })).toBe(NOTICE);
  });

  it("shows nothing for a null, missing, blank or non-string notice", () => {
    expect(stallNotice({ stall_notice: null })).toBeNull();
    expect(stallNotice({})).toBeNull();
    expect(stallNotice(undefined)).toBeNull();
    expect(stallNotice(null)).toBeNull();
    expect(stallNotice({ stall_notice: "" })).toBeNull();
    expect(stallNotice({ stall_notice: "   " })).toBeNull();
    expect(stallNotice({ stall_notice: 42 })).toBeNull();
  });

  it("renders the notice's backticked commands as inline code", () => {
    const code = splitBackticks(stallNotice({ stall_notice: NOTICE })).filter((s) => s.code);
    expect(code.map((s) => s.text)).toEqual(["claude", "/login"]);
  });
});
