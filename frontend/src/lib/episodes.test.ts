import { describe, expect, it } from "vitest";
import { entryCount, episodeName, episodeSpan, isOpen, openCount, sortEpisodes } from "./episodes";

describe("episode timeline", () => {
  it("sorts newest start first, from either answer shape", () => {
    const eps = [
      { id: "a", started_at: 100 },
      { id: "b", started_at: 300 },
      { id: "c", started_at: null },
      { id: "d", started_at: 200 },
    ];
    expect(sortEpisodes({ episodes: eps }).map((e) => e.id)).toEqual(["b", "d", "a", "c"]);
    expect(sortEpisodes({ entries: eps }).map((e) => e.id)).toEqual(["b", "d", "a", "c"]);
    expect(sortEpisodes(null)).toEqual([]);
  });

  it("an episode without an end is open", () => {
    expect(isOpen({ ended_at: null })).toBe(true);
    expect(isOpen({})).toBe(true);
    expect(isOpen({ ended_at: 5 })).toBe(false);
    expect(openCount([{ id: "a" }, { id: "b", ended_at: 2 }])).toBe(1);
  });

  it("names by title, else id", () => {
    expect(episodeName({ id: "ep-1", title: "Console build" })).toBe("Console build");
    expect(episodeName({ id: "ep-1", title: "  " })).toBe("ep-1");
    expect(episodeName({ id: "ep-1" })).toBe("ep-1");
  });

  it("gives a duration, open, or nothing when a bound is missing", () => {
    expect(episodeSpan({ started_at: 1000, ended_at: 1000 + 3 * 3600 + 300 })).toBe("3 h 5 m");
    expect(episodeSpan({ started_at: 1000, ended_at: null })).toBe("open");
    expect(episodeSpan({ started_at: null, ended_at: 2000 })).toBe("");
  });

  it("never turns a missing entry count into zero", () => {
    expect(entryCount(undefined)).toBe("entry count unavailable");
    expect(entryCount(null)).toBe("entry count unavailable");
    expect(entryCount(0)).toBe("0 entries");
    expect(entryCount(1)).toBe("1 entry");
  });
});
