// The knob editor's pure logic: turning a control's raw input into the value
// POST /api/config receives, deciding what counts as an edit against the live
// value, and building the patch and the review diff. Settings writes the
// daemon's config file, so every rule here is exact about what is sent.
//
// A draft is a control's raw state: a boolean for a switch, otherwise the
// string in the field. A knob with no draft is untouched.

import type { ConfigWriteResult, Knob, KnobGroup } from "./api/config";

/**
 * What an edit is measured against: the value saved in config.yaml for a
 * restart knob that was changed but not yet applied (the server sends it as
 * `saved`), else the running value. Measuring a restart knob against its
 * running value made a mistaken save impossible to undo: typing the old
 * value back looked like no change.
 */
export function baseValue(knob: Knob): unknown {
  return Object.hasOwn(knob, "saved") ? knob.saved : knob.value;
}

/** A restart knob whose saved value waits for the next daemon start. */
export function pendingRestart(knob: Knob): boolean {
  return Object.hasOwn(knob, "saved");
}

export type Draft = string | boolean;
export type Drafts = Record<string, Draft>;

export type Coerced =
  /** Send this value. */
  | { kind: "value"; value: unknown }
  /** Not an edit: an emptied number field. */
  | { kind: "none" }
  /** Cannot be sent as typed. */
  | { kind: "invalid"; message: string };

/**
 * A control's raw input as the value to send, mirroring the server's
 * `_coerce` (web/config_io.py) so a refusal is caught before the save:
 * - bool: the switch state;
 * - enum: one of `options`;
 * - string: trimmed, and an empty field is null (it clears the value);
 * - int / float: an emptied field is no edit, not a value; an int must be a
 *   whole number; both must sit inside min and max.
 */
export function coerce(knob: Knob, raw: Draft): Coerced {
  switch (knob.type) {
    case "bool":
      return { kind: "value", value: typeof raw === "boolean" ? raw : raw === "true" };
    case "enum": {
      const v = String(raw);
      if (knob.options && !knob.options.includes(v)) return { kind: "invalid", message: "Pick one of the listed options" };
      return { kind: "value", value: v };
    }
    case "int":
    case "float": {
      const s = String(raw).trim();
      if (s === "") return { kind: "none" };
      const n = Number(s);
      if (!Number.isFinite(n)) return { kind: "invalid", message: "Enter a number" };
      if (knob.type === "int" && !Number.isInteger(n)) return { kind: "invalid", message: "Enter a whole number" };
      if (knob.min !== undefined && knob.min !== null && n < knob.min) {
        return { kind: "invalid", message: `The lowest allowed value is ${knob.min}` };
      }
      if (knob.max !== undefined && knob.max !== null && n > knob.max) {
        return { kind: "invalid", message: `The highest allowed value is ${knob.max}` };
      }
      return { kind: "value", value: n };
    }
    default: {
      const s = String(raw).trim();
      if (s === "") return { kind: "value", value: null };
      if (knob.format === "url" && !/^https?:\/\//i.test(s)) {
        return { kind: "invalid", message: "Enter an http:// or https:// URL" };
      }
      return { kind: "value", value: s };
    }
  }
}

function unsetString(v: unknown): boolean {
  return v === null || v === undefined || (typeof v === "string" && v.trim() === "");
}

/**
 * Whether two values of a knob are the same setting. For a string knob an
 * empty string and null are both "not set", so clearing an already empty
 * field is no edit (several judge knobs serve "" live).
 */
export function sameValue(knob: Knob, a: unknown, b: unknown): boolean {
  switch (knob.type) {
    case "bool":
      return Boolean(a) === Boolean(b);
    case "int":
    case "float":
      if (a === null || a === undefined || b === null || b === undefined) return a == b;
      return Number(a) === Number(b);
    case "enum":
      return String(a ?? "") === String(b ?? "");
    default:
      if (unsetString(a) || unsetString(b)) return unsetString(a) && unsetString(b);
      return String(a).trim() === String(b).trim();
  }
}

/** The raw control state that shows `value`. */
export function draftFromValue(knob: Knob, value: unknown): Draft {
  if (knob.type === "bool") return Boolean(value);
  return value === null || value === undefined ? "" : String(value);
}

/** What a knob's control shows: its draft, else the live value. */
export function controlDraft(knob: Knob, drafts: Drafts): Draft {
  return Object.hasOwn(drafts, knob.path) ? drafts[knob.path] : draftFromValue(knob, baseValue(knob));
}

export type RowState = { state: "clean" } | { state: "edited"; value: unknown } | { state: "invalid"; message: string };

/** One knob's edit state: untouched or back to live, a real edit, or input that cannot be sent. */
export function rowState(knob: Knob, drafts: Drafts): RowState {
  if (!Object.hasOwn(drafts, knob.path)) return { state: "clean" };
  const c = coerce(knob, drafts[knob.path]);
  if (c.kind === "invalid") return { state: "invalid", message: c.message };
  if (c.kind === "none" || sameValue(knob, c.value, baseValue(knob))) return { state: "clean" };
  return { state: "edited", value: c.value };
}

export interface Edit {
  knob: Knob;
  /** The coerced value that will be sent. */
  value: unknown;
}

export interface Pending {
  edits: Edit[];
  invalid: { knob: Knob; message: string }[];
}

/** Every edit and every unsendable field, in the server's group and knob order. */
export function pendingEdits(groups: KnobGroup[], drafts: Drafts): Pending {
  const out: Pending = { edits: [], invalid: [] };
  for (const g of groups) {
    for (const k of g.knobs) {
      const s = rowState(k, drafts);
      if (s.state === "edited") out.edits.push({ knob: k, value: s.value });
      else if (s.state === "invalid") out.invalid.push({ knob: k, message: s.message });
    }
  }
  return out;
}

/** The body's `patch`: dotted path to coerced value, edits only. */
export function buildPatch(edits: Edit[]): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  for (const e of edits) patch[e.knob.path] = e.value;
  return patch;
}

export function needsRestart(edits: Edit[]): boolean {
  return edits.some((e) => e.knob.restart === true);
}

/** A value as the review shows it. */
export function fmtKnobValue(knob: Knob, v: unknown): string {
  if (knob.type === "bool") return v ? "on" : "off";
  if (v === null || v === undefined || (typeof v === "string" && v.trim() === "")) return "not set";
  return String(v);
}

export interface DiffRow {
  path: string;
  label: string;
  old: string;
  new: string;
  restart: boolean;
}

/** The review modal's rows: path, live value, value to send, restart flag. */
export function diffRows(edits: Edit[]): DiffRow[] {
  return edits.map((e) => ({
    path: e.knob.path,
    label: e.knob.label || e.knob.path,
    old: fmtKnobValue(e.knob, baseValue(e.knob)),
    new: fmtKnobValue(e.knob, e.value),
    restart: e.knob.restart === true,
  }));
}

export function hasDefault(knob: Knob): boolean {
  return knob.default !== undefined && knob.default !== null;
}

/** Whether the control already shows the knob's default. */
export function atDefault(knob: Knob, drafts: Drafts): boolean {
  if (!hasDefault(knob)) return true;
  const c = coerce(knob, controlDraft(knob, drafts));
  return c.kind === "value" && sameValue(knob, c.value, knob.default);
}

/** Drafts with one knob's control set to `draft`; a draft equal to the live value is dropped. */
export function withDraft(drafts: Drafts, knob: Knob, draft: Draft): Drafts {
  const next = { ...drafts };
  const c = coerce(knob, draft);
  if (c.kind === "value" && sameValue(knob, c.value, baseValue(knob)) && draft === draftFromValue(knob, baseValue(knob))) {
    delete next[knob.path];
  } else {
    next[knob.path] = draft;
  }
  return next;
}

export function numberStep(knob: Knob): number {
  return knob.step ?? (knob.type === "int" ? 1 : 0.01);
}

/** "0 to 1", "at least 30", or "" when the server gives no range. */
export function rangeText(knob: Knob): string {
  const lo = knob.min ?? null;
  const hi = knob.max ?? null;
  if (lo !== null && hi !== null) return `${lo} to ${hi}`;
  if (lo !== null) return `at least ${lo}`;
  if (hi !== null) return `at most ${hi}`;
  return "";
}

/** "memory.dream.min_batch" -> "dream.min_batch". */
export function pathTail(path: string): string {
  return path.split(".").slice(-2).join(".");
}

/** A stable element id for a group's panel. */
export function groupAnchor(name: string): string {
  return `settings-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "")}`;
}

/** The toast after a save: what applied live and what waits for a restart. */
export function saveMessage(r: ConfigWriteResult): string {
  const live = r.applied?.length ?? 0;
  const restart = r.restart_required?.length ?? 0;
  const parts: string[] = [];
  if (live) parts.push(`${live} applied live`);
  if (restart) parts.push(`${restart} ${restart === 1 ? "needs" : "need"} a daemon restart`);
  return parts.length ? `Settings saved: ${parts.join(", ")}.` : "Settings saved.";
}

/** The backup's file name, without the host's directory. */
export function backupName(path: string | null | undefined): string {
  if (!path) return "";
  return path.split(/[\\/]/).pop() ?? "";
}
