// localStorage wrappers. The keys are the ones the console has always used,
// so a stored token and theme survive upgrades. Storage can be unavailable
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
