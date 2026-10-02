import { describe, expect, it } from "vitest";
import type { Knob, KnobGroup } from "./api/config";
import {
  atDefault,
  backupName,
  buildPatch,
  coerce,
  controlDraft,
  diffRows,
  fmtKnobValue,
  groupAnchor,
  needsRestart,
  numberStep,
  pathTail,
  pendingEdits,
  rangeText,
  rowState,
  sameValue,
  saveMessage,
  withDraft,
} from "./config";

// Shapes as GET /api/config serves them (web/config_io.py read_config):
// keys whose registry value is None are omitted, `value` is always present.
const floatK: Knob = {
  path: "memory.surprise_threshold",
  label: "Surprise / novelty gate",
  type: "float",
  default: 0,
  min: 0,
  max: 1,
  step: 0.01,
  restart: false,
  value: 0,
};
const intK: Knob = { path: "memory.top_k", label: "Top k", type: "int", default: 8, min: 1, max: 50, restart: false, value: 8 };
const restartK: Knob = { path: "memory.reranker.top_n", label: "Rerank top n", type: "int", default: 20, min: 1, max: 100, restart: true, value: 20 };
const boolK: Knob = { path: "memory.hide_superseded", label: "Hide superseded", type: "bool", default: false, restart: false, value: false };
const enumK: Knob = {
  path: "memory.dream.literal_gate",
  label: "Literal-faithfulness gate",
  type: "enum",
  default: "enforce",
  options: ["off", "log", "enforce"],
  restart: false,
  value: "enforce",
};
const strNull: Knob = {
  path: "memory.dream.extractor_model",
  label: "Model name",
  type: "string",
  restart: false,
  suggestions: ["extractor"],
  value: null,
};
const strEmpty: Knob = { path: "memory.deep_dream.judge_model", label: "Judge model", type: "string", restart: false, value: "" };
const urlK: Knob = {
  path: "memory.dream.extractor_base_url",
  label: "Endpoint base URL",
  type: "string",
  format: "url",
  restart: false,
  value: "http://host.docker.internal:8082/v1",
};

const groups: KnobGroup[] = [
  { name: "Retrieval", knobs: [floatK, intK, boolK] },
  { name: "Reranker", knobs: [restartK] },
  { name: "Dream", knobs: [enumK] },
  { name: "Extractor", knobs: [strNull, strEmpty, urlK] },
];

describe("coerce: the value each knob type sends", () => {
  it("a string field trims, and an empty field is null (clears the value)", () => {
    expect(coerce(strNull, "  claude-opus-5  ")).toEqual({ kind: "value", value: "claude-opus-5" });
    expect(coerce(strNull, "")).toEqual({ kind: "value", value: null });
    expect(coerce(strNull, "   ")).toEqual({ kind: "value", value: null });
  });

  it("a url-format string must be http(s), as the server enforces", () => {
    expect(coerce(urlK, "https://example.com/v1")).toEqual({ kind: "value", value: "https://example.com/v1" });
    expect(coerce(urlK, "ftp://example.com").kind).toBe("invalid");
    expect(coerce(urlK, "")).toEqual({ kind: "value", value: null });
  });

  it("an emptied number field is no edit, never a value", () => {
    expect(coerce(intK, "")).toEqual({ kind: "none" });
    expect(coerce(floatK, "  ")).toEqual({ kind: "none" });
  });

  it("int and float parse to JSON numbers; an int must be whole", () => {
    expect(coerce(intK, "12")).toEqual({ kind: "value", value: 12 });
    expect(coerce(floatK, "0.35")).toEqual({ kind: "value", value: 0.35 });
    expect(coerce(floatK, "1")).toEqual({ kind: "value", value: 1 });
    expect(coerce(intK, "3.5")).toEqual({ kind: "invalid", message: "Enter a whole number" });
    expect(coerce(floatK, "abc").kind).toBe("invalid");
  });

  it("numbers outside min and max are refused before the save", () => {
    expect(coerce(intK, "0")).toEqual({ kind: "invalid", message: "The lowest allowed value is 1" });
    expect(coerce(intK, "51")).toEqual({ kind: "invalid", message: "The highest allowed value is 50" });
    expect(coerce(floatK, "1")).toEqual({ kind: "value", value: 1 });
    expect(coerce({ ...intK, min: undefined, max: undefined }, "9999")).toEqual({ kind: "value", value: 9999 });
  });

  it("bool sends the switch state; enum sends one of its options", () => {
    expect(coerce(boolK, true)).toEqual({ kind: "value", value: true });
    expect(coerce(boolK, false)).toEqual({ kind: "value", value: false });
    expect(coerce(enumK, "log")).toEqual({ kind: "value", value: "log" });
    expect(coerce(enumK, "nope").kind).toBe("invalid");
  });
});

describe("dirty detection against the live value", () => {
  it("an untouched knob is clean", () => {
    expect(rowState(intK, {})).toEqual({ state: "clean" });
  });

  it("a number equal to the live value is clean, however it is spelled", () => {
    expect(rowState(floatK, { [floatK.path]: "0.0" })).toEqual({ state: "clean" });
    expect(rowState(intK, { [intK.path]: "8" })).toEqual({ state: "clean" });
    expect(rowState(intK, { [intK.path]: "9" })).toEqual({ state: "edited", value: 9 });
  });

  it("an emptied number field is clean (no edit)", () => {
    expect(rowState(intK, { [intK.path]: "" })).toEqual({ state: "clean" });
  });

  it("clearing an already unset string is no edit, whether live is null or empty", () => {
    expect(rowState(strNull, { [strNull.path]: "" })).toEqual({ state: "clean" });
    expect(rowState(strEmpty, { [strEmpty.path]: "" })).toEqual({ state: "clean" });
    expect(sameValue(strEmpty, null, "")).toBe(true);
  });

  it("clearing a set string is an edit to null", () => {
    expect(rowState(urlK, { [urlK.path]: "" })).toEqual({ state: "edited", value: null });
  });

  it("a restart knob saved but not applied is measured against the saved value", () => {
    // Saved 30 by mistake; the daemon still runs 20 until it restarts.
    const pending: Knob = { ...restartK, value: 20, saved: 30 };
    expect(controlDraft(pending, {})).toBe("30");
    // Typing 20 back is the undo: a real edit, sent as 20.
    expect(rowState(pending, { [pending.path]: "20" })).toEqual({ state: "edited", value: 20 });
    expect(rowState(pending, { [pending.path]: "30" })).toEqual({ state: "clean" });
    // The review shows the file's value as the one being replaced.
    const rows = diffRows([{ knob: pending, value: 20 }]);
    expect(rows[0]).toMatchObject({ old: "30", new: "20" });
    // Without a saved value nothing changes.
    expect(rowState(restartK, { [restartK.path]: "20" })).toEqual({ state: "clean" });
  });

  it("a bool or enum flip is an edit; flipping back is clean", () => {
    expect(rowState(boolK, { [boolK.path]: true })).toEqual({ state: "edited", value: true });
    expect(rowState(boolK, { [boolK.path]: false })).toEqual({ state: "clean" });
    expect(rowState(enumK, { [enumK.path]: "off" })).toEqual({ state: "edited", value: "off" });
  });

  it("unsendable input is reported, not sent", () => {
    expect(rowState(intK, { [intK.path]: "2.5" })).toEqual({ state: "invalid", message: "Enter a whole number" });
  });

  it("withDraft drops a draft that puts the control back on the live value", () => {
    const d1 = withDraft({}, intK, "9");
    expect(d1).toEqual({ [intK.path]: "9" });
    expect(withDraft(d1, intK, "8")).toEqual({});
    expect(withDraft({}, boolK, false)).toEqual({});
  });

  it("the control shows its draft, else the live value", () => {
    expect(controlDraft(intK, {})).toBe("8");
    expect(controlDraft(strNull, {})).toBe("");
    expect(controlDraft(boolK, {})).toBe(false);
    expect(controlDraft(intK, { [intK.path]: "" })).toBe("");
  });
});

describe("the patch built from edits", () => {
  const drafts = {
    [floatK.path]: "0.25",
    [intK.path]: "", // emptied: no edit
    [boolK.path]: false, // same as live: no edit
    [restartK.path]: "30",
    [enumK.path]: "log",
    [strNull.path]: " claude-sonnet-5 ",
    [strEmpty.path]: "", // already unset: no edit
    [urlK.path]: "",
    "memory.not_a_knob": "x", // not served: ignored
  };
  const p = pendingEdits(groups, drafts);

  it("holds exactly the real edits, in server order, with coerced values", () => {
    expect(buildPatch(p.edits)).toEqual({
      "memory.surprise_threshold": 0.25,
      "memory.reranker.top_n": 30,
      "memory.dream.literal_gate": "log",
      "memory.dream.extractor_model": "claude-sonnet-5",
      "memory.dream.extractor_base_url": null,
    });
    expect(Object.keys(buildPatch(p.edits))).toEqual([
      "memory.surprise_threshold",
      "memory.reranker.top_n",
      "memory.dream.literal_gate",
      "memory.dream.extractor_model",
      "memory.dream.extractor_base_url",
    ]);
    expect(p.invalid).toEqual([]);
  });

  it("separates unsendable fields from edits", () => {
    const q = pendingEdits(groups, { [intK.path]: "0", [floatK.path]: "0.5" });
    expect(buildPatch(q.edits)).toEqual({ "memory.surprise_threshold": 0.5 });
    expect(q.invalid.map((i) => i.knob.path)).toEqual(["memory.top_k"]);
  });

  it("is empty when nothing changed", () => {
    expect(pendingEdits(groups, {})).toEqual({ edits: [], invalid: [] });
  });
});

describe("restart-required detection", () => {
  it("is true only when an edited knob is flagged restart", () => {
    expect(needsRestart(pendingEdits(groups, { [intK.path]: "9" }).edits)).toBe(false);
    expect(needsRestart(pendingEdits(groups, { [intK.path]: "9", [restartK.path]: "30" }).edits)).toBe(true);
    expect(needsRestart([])).toBe(false);
  });
});

describe("the review diff rows", () => {
  it("list path, label, live value, new value and the restart flag", () => {
    const edits = pendingEdits(groups, {
      [restartK.path]: "30",
      [boolK.path]: true,
      [urlK.path]: "",
      [strNull.path]: "extractor",
    }).edits;
    expect(diffRows(edits)).toEqual([
      { path: "memory.hide_superseded", label: "Hide superseded", old: "off", new: "on", restart: false },
      { path: "memory.reranker.top_n", label: "Rerank top n", old: "20", new: "30", restart: true },
      { path: "memory.dream.extractor_model", label: "Model name", old: "not set", new: "extractor", restart: false },
      {
        path: "memory.dream.extractor_base_url",
        label: "Endpoint base URL",
        old: "http://host.docker.internal:8082/v1",
        new: "not set",
        restart: false,
      },
    ]);
  });

  it("fall back to the path when a knob has no label", () => {
    const k: Knob = { path: "time.relative_age", type: "bool", value: true };
    expect(diffRows([{ knob: k, value: false }])[0].label).toBe("time.relative_age");
  });
});

describe("defaults, ranges and labels", () => {
  it("knows when the control already shows the default", () => {
    expect(atDefault(intK, {})).toBe(true);
    expect(atDefault(intK, { [intK.path]: "9" })).toBe(false);
    expect(atDefault(strNull, {})).toBe(true); // no default served: nothing to reset to
  });

  it("int steps by 1 and float by 0.01 unless the server says otherwise", () => {
    expect(numberStep({ ...intK, step: undefined })).toBe(1);
    expect(numberStep({ ...floatK, step: undefined })).toBe(0.01);
    expect(numberStep(floatK)).toBe(0.01);
    expect(numberStep({ ...floatK, step: 0.05 })).toBe(0.05);
  });

  it("phrases ranges and values", () => {
    expect(rangeText(floatK)).toBe("0 to 1");
    expect(rangeText({ ...intK, max: undefined })).toBe("at least 1");
    expect(rangeText(strNull)).toBe("");
    expect(fmtKnobValue(strEmpty, "")).toBe("not set");
    expect(fmtKnobValue(floatK, 0)).toBe("0");
    expect(pathTail("memory.dream.min_batch")).toBe("dream.min_batch");
    expect(groupAnchor("Retrieval log")).toBe("settings-retrieval-log");
  });
});

describe("save outcome", () => {
  it("names what applied live and what needs a restart", () => {
    expect(saveMessage({ applied: ["a", "b"], restart_required: ["c"] })).toBe(
      "Settings saved: 2 applied live, 1 needs a daemon restart.",
    );
    expect(saveMessage({ applied: [], restart_required: ["c", "d"] })).toBe("Settings saved: 2 need a daemon restart.");
    expect(saveMessage({})).toBe("Settings saved.");
  });

  it("shows only the backup's file name", () => {
    expect(backupName("C:\\data\\config.yaml.20261002-101500.abc.bak")).toBe("config.yaml.20261002-101500.abc.bak");
    expect(backupName("/data/config.yaml.20261002-101500.abc.bak")).toBe("config.yaml.20261002-101500.abc.bak");
    expect(backupName(null)).toBe("");
  });
});
