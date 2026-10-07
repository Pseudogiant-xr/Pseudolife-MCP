import { SCRUB_MAX } from "./graph";

// User-requested visibility replay is independent of the galaxy's automatic
// simulation and camera motion. It always starts at the oldest time cut.
export function createTimeReplay(apply: (v: number) => void, onplaying: (active: boolean) => void) {
  let timer: ReturnType<typeof setInterval> | undefined;

  function stop() {
    clearInterval(timer);
    timer = undefined;
    onplaying(false);
  }

  function toggle() {
    if (timer !== undefined) {
      stop();
      return;
    }
    let x = 0;
    apply(x);
    onplaying(true);
    timer = setInterval(() => {
      x = Math.min(x + 12, SCRUB_MAX);
      if (x === SCRUB_MAX) stop();
      apply(x);
    }, 100);
  }

  return { toggle, stop };
}
