// The browser side of Console sign-in: the #/signin route a sign-in link
// opens, the sign-in dialog's code form and passkey choice, and the start-up
// check of a pending token an interrupted sign-in left behind. The decisions live in signin.ts;
// this module wires them to fetch, localStorage, the address bar and the UI.

import { api } from "./api/client";
import { DEFAULT_ROUTE } from "./nav";
import { toast } from "./overlay.svelte";
import {
  canMint,
  currentSignin,
  landingHash,
  LINK_QUESTION,
  MESSAGES,
  redeem,
  redeemPasskey,
  replaceQuestion,
  settlePending,
  signinCapability,
  signinTarget,
  type RedeemResult,
  type SigninDeps,
} from "./signin";
import { sign } from "./maintainerFlow.svelte";
import { readKey, TOKEN_KEY, writeKey } from "./storage";
import { refresh, ui } from "./state.svelte";

export type Tone = "info" | "warn" | "danger";

export const signinFlow = $state({
  busy: false,
  /** The last outcome worth showing in the dialog, in plain words. */
  message: "",
  tone: "info" as Tone,
  /** Set while a sign-in link waits for the person's go-ahead. */
  question: null as string | null,
  /** Whether that question replaces a working sign-in (else it signs in afresh). */
  replacing: false,
});

function deps(): SigninDeps {
  return {
    fetch: (url, init) => fetch(url, init),
    store: { read: readKey, write: writeKey },
    crypto: globalThis.crypto,
    userAgent: typeof navigator === "undefined" ? "" : navigator.userAgent,
  };
}

function say(message: string, tone: Tone = "info"): void {
  signinFlow.message = message;
  signinFlow.tone = tone;
}

let answer: ((ok: boolean) => void) | null = null;

function ask(question: string, replacing: boolean): Promise<boolean> {
  answer?.(false);
  signinFlow.question = question;
  signinFlow.replacing = replacing;
  return new Promise((resolve) => {
    answer = resolve;
  });
}

/** The dialog's Replace / Keep buttons, and closing the dialog (keep). */
export function answerReplace(ok: boolean): void {
  const a = answer;
  answer = null;
  signinFlow.question = null;
  a?.(ok);
}

/** Clear the last outcome when the dialog opens afresh. */
export function resetSigninMessage(): void {
  if (!signinFlow.busy) say("");
}

function finish(r: RedeemResult): void {
  if (r.kind === "signed_in") {
    say(r.principal ? `Signed in as ${r.principal}. Reloading.` : "Signed in. Reloading.");
    location.reload();
    return;
  }
  say(r.message, r.kind === "cancelled" ? "info" : r.kind === "unconfirmed" ? "warn" : "danger");
}

/** The dialog's "Sign in with a code". The person typed it here, so nothing is asked first. */
export async function signInWithCode(raw: string): Promise<void> {
  if (signinFlow.busy) return;
  signinFlow.busy = true;
  say("Signing in.");
  try {
    await settleOnStart();
    finish(await redeem(raw, deps()));
  } finally {
    signinFlow.busy = false;
  }
}

/**
 * The dialog's "Sign in with your passkey". The person clicked it here, so
 * nothing is asked first, even over a working sign-in.
 */
export async function signInWithPasskey(): Promise<void> {
  if (signinFlow.busy) return;
  signinFlow.busy = true;
  say("Waiting for your passkey.");
  try {
    await settleOnStart();
    if (!ui.tokenOpen) return;
    const r = await redeemPasskey({ ...deps(), sign, cancelled: () => !ui.tokenOpen });
    if (r.kind === "cancelled" && !ui.tokenOpen) return;
    finish(r);
  } finally {
    signinFlow.busy = false;
  }
}

/**
 * A sign-in link (#/signin?code=…&to=…): the code leaves the address bar at
 * once, the Console lands on the target (an allowed route) or the default,
 * and the sign-in runs in the dialog. A browser already signed in is asked
 * before the link replaces that sign-in.
 */
export function takeSigninRoute(): void {
  const { code, to } = ui.query;
  const target = signinTarget(to);
  history.replaceState(history.state, "", landingHash(target));
  ui.route = target ?? DEFAULT_ROUTE;
  ui.query = {};
  ui.tokenOpen = true;
  if (code) void signInWithLink(code);
}

async function signInWithLink(code: string): Promise<void> {
  if (signinFlow.busy) return;
  signinFlow.busy = true;
  say("Signing in.");
  // Closing the dialog cancels the link: checked after every wait, so a
  // closed dialog never gets a question or a redemption later.
  const closed = () => !ui.tokenOpen;
  try {
    await settleOnStart();
    if (closed()) return;
    let health: unknown = null;
    try {
      health = await api.health();
      if (closed()) return;
    } catch {
      say("The daemon did not answer, so this link could not sign in. Check that it is running, then open the link again.", "danger");
      return;
    }
    const cap = signinCapability(health);
    if (!cap?.link) {
      say(
        cap
          ? "This daemon does not offer sign-in with a link. Use Advanced below."
          : "This daemon cannot sign in with a link yet: it needs an update. Use Advanced below meanwhile.",
        "warn",
      );
      return;
    }
    if (!canMint(globalThis.crypto)) {
      say(MESSAGES.unsupported, "warn");
      return;
    }
    const current = await currentSignin(deps());
    if (closed()) return;
    // Always a click before a link signs in: a link someone else sent must
    // not sign this browser in as them (login CSRF), and must not replace a
    // working sign-in.
    const replacing = !!current && current.state !== "refused";
    say("");
    const go = await ask(
      replacing ? replaceQuestion(current.state === "works" ? current.principal : null) : LINK_QUESTION,
      replacing,
    );
    if (!go || closed()) {
      ui.tokenOpen = false;
      if (replacing) toast("Kept this browser's sign-in. The link was not used.", "info");
      return;
    }
    say("Signing in.");
    finish(await redeem(code, deps()));
  } finally {
    signinFlow.busy = false;
  }
}

let settling: Promise<void> | null = null;

/**
 * On start: settle a pending token an interrupted sign-in left behind. Runs
 * once per page load; a sign-in started meanwhile waits for it, so the two
 * never race over the pending key.
 */
export function settleOnStart(): Promise<void> {
  settling ??= (async () => {
    const r = await settlePending(deps());
    if (r === "promoted") {
      ui.hasToken = readKey(TOKEN_KEY) !== "";
      refresh();
      toast("Signed in");
    } else if (r === "dropped") {
      toast("An earlier sign-in did not complete. Sign in again if this Console asks.", "warn", 6000);
    }
  })().catch(() => undefined);
  return settling;
}
