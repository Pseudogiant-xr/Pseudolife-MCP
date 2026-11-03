import { describe, expect, it } from "vitest";
import { ApiError } from "./api/client";
import { actionError, explainError } from "./errors";

describe("explainError", () => {
  it("tells an invited machine's sign-in that only the daemon's host changes daemon settings", () => {
    // POST /api/config from a stored (invited) principal: the daemon has a
    // token, so the tokenless-loopback explanation would be wrong.
    const e = new ApiError(403, "operator_principal_required", { error: "operator_principal_required" });
    const x = explainError(e, "Saving the config");
    expect(x.title).toBe("Only a sign-in from the daemon's host can change this");
    expect(x.body).toContain("invited machine");
    expect(x.body).toContain("pseudolife-mcp console");
    expect(x.body).not.toMatch(/bearer|PSEUDOLIFE_MCP_TOKEN/i);
    expect(x.body).not.toMatch(/tokenless|loopback/i);
    expect(x.token).toBe(true);
    expect(actionError(e, "Save")).toBe("Save failed: only a sign-in from the daemon's host can change this");
  });

  it("explains principals_unavailable as a temporary daemon-side check failure", () => {
    const e = new ApiError(503, "principals_unavailable", { error: "principals_unavailable" });
    const x = explainError(e);
    expect(x.title).toBe("The daemon cannot check tokens right now");
    expect(x.body).toContain("database");
    expect(x.body).not.toContain("HTTP 503");
    expect(x.token).toBe(false);
  });

  it("keeps the tokenless-daemon explanation for the browser gate's 403", () => {
    const hint = "tokenless /api serves loopback browsers only; set PSEUDOLIFE_MCP_TOKEN for remote access";
    const gated = explainError(new ApiError(403, "forbidden_host", { error: "forbidden_host", hint }));
    expect(gated.body).toBe(hint);
    const bare = explainError(new ApiError(403, "http_403", null), "This view");
    expect(bare.title).toBe("This view was refused");
    expect(bare.body).toContain("tokenless daemon");
  });

  it("says to sign in again, and that a new address needs its own sign-in", () => {
    // The Console keeps its sign-in per address, so the passkey HTTPS address
    // opens with none even where the usual address works.
    const x = explainError(new ApiError(401, "unauthorized", { error: "unauthorized" }));
    expect(x.title).toBe("This Console is not signed in");
    expect(x.body).toMatch(/each address/i);
    expect(x.body).toContain("pseudolife-mcp console");
    expect(x.body).not.toMatch(/bearer/i);
    expect(x.token).toBe(true);
  });
});
