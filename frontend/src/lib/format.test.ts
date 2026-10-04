import { describe, expect, it } from "vitest";
import { KEY_PREFIX_LEN, keyPrefix } from "./format";

describe("keyPrefix", () => {
  it("shows a passkey id as the host prints it: its first 12 characters", () => {
    // tests/test_console_build.py pins KEY_PREFIX_LEN to the host's constant.
    expect(KEY_PREFIX_LEN).toBe(12);
    expect(keyPrefix("CqsQqbXRP_d5wLx9-2")).toBe("CqsQqbXRP_d5");
    expect(keyPrefix("short")).toBe("short");
    expect(keyPrefix(null)).toBe("");
  });
});
