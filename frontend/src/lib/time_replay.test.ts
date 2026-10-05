import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SCRUB_MAX, sliderCut } from "./graph";
import { createTimeReplay } from "./time_replay";

describe("explicit graph replay", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function fixture() {
    let v = SCRUB_MAX;
    const cuts: (number | null)[] = [];
    const playing = vi.fn();
    const replay = createTimeReplay((next) => {
      v = next;
      cuts.push(sliderCut(next, { t0: 100, t1: 1100 }));
    }, playing);
    return { replay, cuts, playing, value: () => v };
  }

  it("resets a previously scrubbed position and applies the oldest cut before the first tick", () => {
    const f = fixture();
    f.replay.toggle();
    expect(f.value()).toBe(0);
    expect(f.cuts).toEqual([100]);
    expect(f.playing).toHaveBeenLastCalledWith(true);
    vi.advanceTimersByTime(100);
    expect(f.cuts).toEqual([100, 112]);
    f.replay.stop();
    f.replay.toggle();
    expect(f.value()).toBe(0);
    expect(f.cuts.at(-1)).toBe(100);
  });

  it("pauses on a second click and leaves a manual cut unchanged after stopping", () => {
    const f = fixture();
    f.replay.toggle();
    vi.advanceTimersByTime(300);
    f.replay.toggle();
    const paused = [...f.cuts];
    vi.advanceTimersByTime(500);
    expect(f.cuts).toEqual(paused);
    expect(f.playing).toHaveBeenLastCalledWith(false);
    f.replay.toggle();
    f.replay.stop();
    f.cuts.push(750);
    vi.advanceTimersByTime(500);
    expect(f.cuts.at(-1)).toBe(750);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("finishes at Now with no live timer, and stopping on unmount cancels replay", () => {
    const f = fixture();
    f.replay.toggle();
    vi.advanceTimersByTime(8400);
    expect(f.value()).toBe(SCRUB_MAX);
    expect(f.cuts.at(-1)).toBeNull();
    expect(f.playing).toHaveBeenLastCalledWith(false);
    expect(vi.getTimerCount()).toBe(0);
    f.replay.toggle();
    f.replay.stop();
    const calls = f.cuts.length;
    vi.advanceTimersByTime(1000);
    expect(f.cuts).toHaveLength(calls);
  });
});
