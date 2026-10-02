// Settings: the daemon's knob whitelist (web/config_io.py KNOBS) and the
// dreamer's effective extractor resolution. GET /api/config serves every
// label, help line, range and option; the console hard-codes none of them.

import { get, post } from "./client";
import type { Epoch } from "./types";

export type KnobType = "bool" | "int" | "float" | "enum" | "string";

/** One editable knob. Keys the server has no value for are left out. */
export interface Knob {
  path: string;
  label?: string;
  type: KnobType;
  default?: unknown;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
  restart?: boolean;
  help?: string;
  /** "url": the server refuses anything but an http(s) URL. */
  format?: string;
  suggestions?: string[];
  /** The live effective value in the running daemon (may be null). */
  value: unknown;
  /** Restart knobs only: the value saved in config.yaml for the next start,
   *  present when it differs from the running `value`. */
  saved?: unknown;
}

export interface KnobGroup {
  name: string;
  knobs: Knob[];
}

/** GET /api/config */
export interface ConfigResponse {
  config_path?: string;
  groups: KnobGroup[];
}

/** POST /api/config */
export interface ConfigWriteResult {
  applied?: string[];
  restart_required?: string[];
  config_path?: string;
  /** Absolute path of the timestamped backup, null on a first write. */
  backup?: string | null;
  error?: string;
}

/** The extractor fields of GET /api/dream/status the Dreamer card reads. */
export interface DreamerStatus {
  extractor_mode?: string;
  primary_url?: string | null;
  primary_model?: string | null;
  /** The concrete model behind a launch-default alias, else null. */
  primary_model_served?: string | null;
  fallback_url?: string | null;
  fallback_model?: string | null;
  extractor_source?: string | null;
  model_override?: string | null;
  reasoning_effort?: string | null;
  /** true/false when a fallback is configured and the primary was probed; null otherwise. */
  primary_healthy?: boolean | null;
  last_dream_extractor?: { which?: string; base_url?: string | null; at?: Epoch | null } | null;
}

export type ConfigPatch = Record<string, unknown>;

export const configApi = {
  read: () => get<ConfigResponse>("/api/config"),
  dreamStatus: () => get<DreamerStatus>("/api/dream/status"),
  /** Writes config.yaml on the daemon host (atomic, with a backup). */
  write: (patch: ConfigPatch) => post<ConfigWriteResult>("/api/config", { patch }),
};
