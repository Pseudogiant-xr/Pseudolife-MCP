import { describe, expect, it } from "vitest";
import type { Lesson } from "./api/facts";
import { filterLessons, groupByTask, polarityCounts, polarityOf } from "./lessons";

const l = (over: Partial<Lesson>): Lesson => ({ task: "t", lesson: "x", ...over });

describe("lessons", () => {
  it("read polarity, falling back to the outcome", () => {
    expect(polarityOf({ polarity: "-" })).toBe("-");
    expect(polarityOf({ polarity: "+", outcome: "failure" })).toBe("+");
    expect(polarityOf({ outcome: "failure" })).toBe("-");
    expect(polarityOf({ outcome: "success" })).toBe("+");
    expect(polarityOf({})).toBe("+");
  });

  it("filter on task, aspect, lesson and about, and by polarity", () => {
    const rows = [
      l({ task: "deploy", aspect: "pitfall", lesson: "Never down -v", about: "compose", polarity: "-" }),
      l({ task: "deploy", aspect: "approach", lesson: "Rebuild the daemon only", polarity: "+" }),
      l({ task: "review", lesson: "Search memory first", outcome: "success" }),
    ];
    expect(filterLessons(rows, "COMPOSE", "all")).toHaveLength(1);
    expect(filterLessons(rows, "pitfall", "all")).toHaveLength(1);
    expect(filterLessons(rows, "", "-")).toHaveLength(1);
    expect(filterLessons(rows, "deploy", "+")).toHaveLength(1);
    expect(polarityCounts(rows)).toEqual({ do: 2, avoid: 1 });
  });

  it("group by task, tasks sorted, order kept within a task", () => {
    const g = groupByTask([l({ task: "b", lesson: "1" }), l({ task: "a", lesson: "2" }), l({ task: "b", lesson: "3" })]);
    expect(g.map((x) => x.task)).toEqual(["a", "b"]);
    expect(g[1].lessons.map((x) => x.lesson)).toEqual(["1", "3"]);
  });
});
