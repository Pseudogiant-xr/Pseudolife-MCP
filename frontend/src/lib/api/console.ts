// Console sessions (signed-in browsers): the list Settings shows and revoke,
// and binding a passkey to sign in. All are bearer-gated, so a 401 opens the
// sign-in dialog as everywhere. (Signing in itself is pre-auth: signin.ts.)

import { get, post } from "./client";
import type { ChallengeAnswer, Signed } from "./maintainer";
import { PASSKEY_BIND_PATH, REVOKE_PATH, SESSIONS_PATH, type BindResult, type ConsoleSessions } from "../signin";

export const consoleApi = {
  sessions: () => get<ConsoleSessions>(SESSIONS_PATH),
  revoke: (sessionId: string) => post<{ revoked?: boolean; error?: string }>(REVOKE_PATH, { session_id: sessionId }),
  /** "Use this passkey to sign in", step 1: the bind-sign-in challenge. */
  bindChallenge: () => post<ChallengeAnswer>(PASSKEY_BIND_PATH, {}),
  /** Step 2: the signed bind; answers which principal the key now signs in as. */
  bind: (s: Signed) => post<BindResult>(PASSKEY_BIND_PATH, { payload: s.payload, mac: s.mac, assertion: s.assertion }),
};
