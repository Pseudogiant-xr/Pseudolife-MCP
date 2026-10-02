// Runs a ReviewAction: asks what planCalls() says to ask, POSTs the calls in
// order, reports each failure (including HTTP 200 {error} refusals), and
// toasts the outcome. Resolves to the number of calls the daemon answered,
// done or refused: a refusal such as not_pending or stale_review means the
// item already moved, so the caller reloads either way. 0 means nothing was
// sent or nothing got through. <ReviewAsk/> in App.svelte
// renders the two pickers a plain confirm cannot.

import { post } from "./api/client";
import { actionError } from "./errors";
import { confirm, toast } from "./overlay.svelte";
import { planCalls, refusalOf, type ReviewAction } from "./reviewActions";

export type Pick =
  | { type: "pick-survivor"; a: string; b: string }
  | { type: "pick-project"; count: number; projects: string[] };

interface PendingPick {
  pick: Pick;
  resolve: (answer: string | null) => void;
}

export const pickState = $state<{ current: PendingPick | null }>({ current: null });

function ask(pick: Pick): Promise<string | null> {
  pickState.current?.resolve(null);
  return new Promise((resolve) => {
    pickState.current = { pick, resolve };
  });
}

export function settlePick(answer: string | null): void {
  const p = pickState.current;
  pickState.current = null;
  p?.resolve(answer);
}

/** The last path segment, as a short name for a failed call ("unrelate"). */
function callName(path: string): string {
  return path.split("/").pop() ?? path;
}

export async function runReviewAction(action: ReviewAction, opts: { projects?: string[] } = {}): Promise<number> {
  const plan = planCalls(action);
  let answer: string | undefined;
  const a = plan.ask;
  if (a.type === "confirm") {
    const ok = await confirm({ title: a.title, message: a.message, confirmLabel: a.confirmLabel, danger: a.danger });
    if (!ok) return 0;
  } else if (a.type === "pick-survivor") {
    const got = await ask(a);
    if (got === null) return 0;
    answer = got;
  } else if (a.type === "pick-project") {
    const got = await ask({ type: "pick-project", count: a.count, projects: opts.projects ?? [] });
    if (got === null || !got.trim()) return 0;
    answer = got;
  }

  const calls = plan.calls(answer);
  let ok = 0;
  let answered = 0;
  for (const c of calls) {
    try {
      const r = await post<unknown>(c.path, c.body);
      const refused = refusalOf(r);
      if (refused) {
        answered += 1;
        toast(`${callName(c.path)} was refused: ${refused.replace(/_/g, " ")}`, "danger", 6000);
        if (plan.chained) break;
        continue;
      }
      ok += 1;
      answered += 1;
    } catch (e) {
      toast(actionError(e, callName(c.path)), "danger", 6000);
      if (plan.chained) break;
    }
  }
  if (ok) toast(calls.length > 1 ? `${plan.done} (${ok} of ${calls.length})` : plan.done, "ok");
  return answered;
}
