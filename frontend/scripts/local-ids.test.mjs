import { describe, expect, it } from "vitest";
import { localIdentifiers } from "./local-ids.mjs";

describe("check-dist: which local names count as identifiers", () => {
  it("checks a real user and host name", () => {
    expect(localIdentifiers({ username: "alice", host: "alice-laptop", ci: false })).toEqual({
      check: ["alice", "alice-laptop"],
      skipped: [],
    });
  });

  it("skips generic account names, which identify nobody and occur in any bundle", () => {
    // "dev" occurs in every Svelte bundle (https://svelte.dev/e/...).
    expect(localIdentifiers({ username: "dev", host: "alice-box", ci: false })).toEqual({
      check: ["alice-box"],
      skipped: ["dev"],
    });
    expect(localIdentifiers({ username: "Root", host: "localhost", ci: false }).check).toEqual([]);
  });

  it("checks nothing on CI, and ignores names shorter than three letters", () => {
    expect(localIdentifiers({ username: "alice", host: "alice-laptop", ci: true }).check).toEqual([]);
    expect(localIdentifiers({ username: "al", host: "", ci: false }).check).toEqual([]);
  });
});
