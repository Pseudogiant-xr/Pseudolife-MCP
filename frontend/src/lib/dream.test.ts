import { describe, expect, it } from "vitest";
import { describeDreamRun } from "./dream";

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
