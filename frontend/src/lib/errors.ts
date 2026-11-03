// Plain-language explanations for refused or failed API calls. Each names
// the fix; `token` marks the ones the sign-in dialog can resolve.

import type { ApiError } from "./api/client";
import type { BoardSnapshot } from "./api/types";

export interface Explained {
  title: string;
  body: string;
  /** Offer "Sign in" (the sign-in dialog) next to the message. */
  token: boolean;
}

export function explainError(e: ApiError, what = "This view"): Explained {
  switch (e.code) {
    case "network":
      return {
        title: "The daemon did not answer",
        body: "Check that the daemon is running and reachable from this browser, then refresh.",
        token: false,
      };
    case "token_not_latin1":
      return {
        title: "The stored token has a character a browser cannot send",
        body: "Bearer tokens must be plain ASCII. Paste the token again, without quotes, spaces or smart punctuation.",
        token: true,
      };
    case "unauthorized":
      return {
        title: "This Console is not signed in",
        body: "Sign in again: run `pseudolife-mcp console` on a machine that already uses Pseudolife, and it opens a signed-in Console or shows a code to type here. A browser keeps its sign-in for each address separately, so a new address, such as the HTTPS one for passkeys, needs its own sign-in once.",
        token: true,
      };
    case "authentication_required":
      return {
        title: "The board needs a bearer token on the daemon",
        body: "Coordination requires a configured token even on a loopback install. Start the daemon with PSEUDOLIFE_MCP_TOKEN (or PSEUDOLIFE_MCP_TOKENS), then sign in here.",
        token: true,
      };
    case "principal_not_allowed":
      return {
        title: "This Console's principal may not read the board",
        body: "Add the principal to coordination.allowed_principals in the daemon config, or sign in as an allowed principal.",
        token: true,
      };
    case "operator_principal_required":
      return {
        title: "Only a sign-in from the daemon's host can change this",
        body: "This Console was signed in from an invited machine. It can read and change memory and the board, but not the daemon's settings. To change them, run `pseudolife-mcp console` on the daemon's host and sign in with the link it opens.",
        token: true,
      };
    case "principals_unavailable":
      return {
        title: "The daemon cannot check tokens right now",
        body: "Its database is not answering, so it cannot confirm invited machines' tokens, and nothing ran. Refresh in a moment; if it persists, check the daemon log and the database.",
        token: false,
      };
    case "coordination_unavailable":
      return {
        title: "The board's storage is unavailable",
        body: "The daemon could not read the coordination tables. Check the daemon log and the database, then refresh.",
        token: false,
      };
    default:
      if (e.status === 403) {
        return {
          title: `${what} was refused`,
          body: e.body?.hint ?? "The daemon refused this browser. A tokenless daemon serves loopback browsers only.",
          token: true,
        };
      }
      return {
        title: `${what} could not load`,
        body: `The daemon answered with ${e.status ? `HTTP ${e.status}, ` : ""}${e.code}.`,
        token: false,
      };
  }
}

/**
 * One line for a toast after a failed action: the daemon's own detail when it
 * gave one (a 400's ValueError text), else the explained title.
 */
export function actionError(e: unknown, action: string): string {
  if (e instanceof Error && e.name === "ApiError") {
    const err = e as ApiError;
    const detail = err.body?.detail ?? (err.status === 400 ? err.code : "");
    return detail ? `${action} failed: ${detail}` : `${action} failed: ${explainError(err).title.toLowerCase()}`;
  }
  return `${action} failed.`;
}

/** A board answer that is well formed but has nothing to show. */
export function explainBoardUnavailable(s: BoardSnapshot): Explained | null {
  if (!s.enabled || s.reason === "disabled") {
    return {
      title: "The coordination board is turned off",
      body: "Enable it with coordination.enabled in the daemon config, then restart the daemon.",
      token: false,
    };
  }
  if (!s.available) {
    return {
      title: "The board is not ready yet",
      body:
        s.reason === "not_initialized"
          ? "The daemon's storage has not initialized. It usually is within a few seconds of start; refresh to check again."
          : `The daemon reports the board as unavailable (${s.reason ?? "no reason given"}).`,
      token: false,
    };
  }
  return null;
}
