// localStorage wrappers. Keys are shared with the classic console, so one
// token and one theme choice work in both. Storage can be unavailable
// (private window, blocked site data): every access is guarded.

export const TOKEN_KEY = "pl_token";
export const THEME_KEY = "pl_theme";

export function readKey(key: string): string {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

export function writeKey(key: string, value: string): void {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    // Not persisted; the in-memory value still applies for this tab.
  }
}
