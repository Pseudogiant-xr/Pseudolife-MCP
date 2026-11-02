// Console sessions (signed-in browsers): the list Settings shows and revoke.
// Both are bearer-gated, so a 401 opens the sign-in dialog as everywhere.

import { get, post } from "./client";
import { REVOKE_PATH, SESSIONS_PATH, type ConsoleSessions } from "../signin";

export const consoleApi = {
  sessions: () => get<ConsoleSessions>(SESSIONS_PATH),
  revoke: (sessionId: string) => post<{ revoked?: boolean; error?: string }>(REVOKE_PATH, { session_id: sessionId }),
};
