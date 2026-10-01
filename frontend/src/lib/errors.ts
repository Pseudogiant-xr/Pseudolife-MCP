// Plain-language explanations for refused or failed API calls. Each names
// the fix; `token` marks the ones the token modal can resolve.

import type { ApiError } from "./api/client";
import type { BoardSnapshot } from "./api/types";

export interface Explained {
  title: string;
  body: string;
  /** Offer "Set a bearer token" next to the message. */
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
    case "unauthorized":
      return {
        title: "The daemon did not accept this console's token",
        body: "Store the bearer token the daemon was started with (PSEUDOLIFE_MCP_TOKEN, or one from PSEUDOLIFE_MCP_TOKENS).",
        token: true,
      };
    case "authentication_required":
      return {
        title: "The board needs a bearer token on the daemon",
        body: "Coordination requires a configured token even on a loopback install. Start the daemon with PSEUDOLIFE_MCP_TOKEN (or PSEUDOLIFE_MCP_TOKENS), then store the same token here.",
        token: true,
      };
    case "principal_not_allowed":
      return {
        title: "This token's principal may not read the board",
        body: "Add the principal to coordination.allowed_principals in the daemon config, or use a token for an allowed principal.",
        token: true,
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
