import { afterEach, describe, expect, it, vi } from "vitest";
import { loadEvidence } from "./api/re_evidence";

afterEach(() => vi.unstubAllGlobals());

describe("RE proof dashboard transport", () => {
  it("keeps the selected project/build and status on the authenticated read", async () => {
    vi.stubGlobal("location", { origin: "https://example.com" });
    vi.stubGlobal("localStorage", { getItem: () => "fixture-bearer" });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      read_only: true, scopes: [], selection: null,
      totals: { artifacts: 0, claims: {} }, artifacts: [], claims: [],
    })));
    vi.stubGlobal("fetch", fetch);

    const result = await loadEvidence("project & one", "client:sha256:abc", "  00b72870  ", "verified");
    const [path, options] = fetch.mock.calls[0];
    const url = new URL(path, "https://example.com");
    expect(url.pathname).toBe("/api/re-evidence");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      project: "project & one", binary_id: "client:sha256:abc",
      q: "00b72870", status: "verified", limit: "250",
    });
    expect(options.method).toBe("GET");
    expect(options.body).toBeUndefined();
    expect(options.headers.Authorization).toBe("Bearer fixture-bearer");
    expect(result.read_only).toBe(true);
  });
});
