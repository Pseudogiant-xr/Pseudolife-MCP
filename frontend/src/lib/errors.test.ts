import { describe, expect, it } from "vitest";
import { ApiError } from "./api/client";
import { actionError, explainError } from "./errors";

describe("explainError", () => {
  it("tells an invited machine that only the operator token changes daemon settings", () => {
    // POST /api/config from a stored (invited) principal: the daemon has a
    // token, so the tokenless-loopback explanation would be wrong.
    const e = new ApiError(403, "operator_principal_required", { error: "operator_principal_required" });
    const x = explainError(e, "Saving the config");
    expect(x.title).toBe("Only the operator's token can change this");
    expect(x.body).toContain("invited machine");
    expect(x.body).not.toMatch(/tokenless|loopback/i);
    expect(x.token).toBe(true);
    expect(actionError(e, "Save")).toBe("Save failed: only the operator's token can change this");
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
});
