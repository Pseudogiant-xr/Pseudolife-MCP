// Pure logic behind the Lessons view: polarity, filter and grouping by task.
// Unit tested in lessons.test.ts.

import type { Lesson } from "./api/facts";

export type Polarity = "+" | "-";
export type PolarityFilter = "all" | Polarity;

/** "+" (do) or "-" (avoid). An unset polarity falls back to the outcome:
 *  a failure is something to avoid, anything else something to do. */
export function polarityOf(l: Pick<Lesson, "polarity" | "outcome">): Polarity {
  if (l.polarity === "+" || l.polarity === "-") return l.polarity;
  return l.outcome === "failure" ? "-" : "+";
}

/** Case-insensitive match over "task aspect lesson about". */
export function lessonMatches(l: Lesson, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return `${l.task} ${l.aspect ?? ""} ${l.lesson} ${l.about ?? ""}`.toLowerCase().includes(needle);
}

export function filterLessons(entries: Lesson[], q: string, pol: PolarityFilter): Lesson[] {
  return entries.filter((l) => (pol === "all" || polarityOf(l) === pol) && lessonMatches(l, q));
}

export interface TaskGroup {
  task: string;
  lessons: Lesson[];
}

/** Grouped by task, tasks sorted by name; lessons keep the daemon's order within a task. */
export function groupByTask(entries: Lesson[]): TaskGroup[] {
  const m = new Map<string, Lesson[]>();
  for (const l of entries) {
    const list = m.get(l.task);
    if (list) list.push(l);
    else m.set(l.task, [l]);
  }
  return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([task, lessons]) => ({ task, lessons }));
}

export function polarityCounts(entries: Lesson[]): { do: number; avoid: number } {
  let pos = 0;
  for (const l of entries) if (polarityOf(l) === "+") pos += 1;
  return { do: pos, avoid: entries.length - pos };
}
