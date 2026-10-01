// Formatting helpers. Ages are words in Geist, never mono; numbers use a
// thin grouping space like the artboards ("2 158").

export function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "";
  return Math.round(n)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, "\u202F");
}

export function fmtDecimal(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "";
  return n.toFixed(digits);
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${fmtNum(n)} ${n === 1 ? one : many}`;
}

/** Seconds as a compact duration: "45 s", "12 m", "3 h 5 m", "2 d 4 h". */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} m` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

/** Short age of an epoch-seconds timestamp: "now", "2 m", "41 m", "3 h", "2 d". */
export function fmtAgeShort(ts: number | null | undefined, nowMs = Date.now()): string {
  if (!ts) return "";
  const s = nowMs / 1000 - ts;
  if (s < 60) return "now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h`;
  const d = Math.floor(h / 24);
  if (d < 60) return `${d} d`;
  return `${Math.floor(d / 30)} mo`;
}

/** Relative phrase for a past or future epoch: "3 m ago", "in 9 h 12 m". */
export function fmtRelative(ts: number | null | undefined, nowMs = Date.now()): string {
  if (!ts) return "";
  const delta = ts - nowMs / 1000;
  if (Math.abs(delta) < 60) return "just now";
  return delta > 0 ? `in ${fmtDuration(delta)}` : `${fmtDuration(-delta)} ago`;
}

/** Local wall-clock time, "14:50"; adds the date when not today. */
export function fmtClock(ts: number | null | undefined, nowMs = Date.now()): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const today = new Date(nowMs);
  if (d.toDateString() === today.toDateString()) return hm;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${hm}`;
}

export function shortId(id: string | null | undefined, len = 8): string {
  if (!id) return "";
  return id.length > len ? id.slice(0, len) : id;
}

/** "needs_approval" -> "needs approval". */
export function words(code: string | null | undefined): string {
  return (code ?? "").replace(/_/g, " ").trim();
}
