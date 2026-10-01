import { describe, expect, it } from "vitest";
import {
  buildColors,
  communityHue,
  GALAXY_HUES,
  etypeHsl,
  hslCss,
  labelElement,
  linkLabelText,
  projectHue,
  recencyLightness,
  UNATTRIBUTED,
} from "./galaxy";

describe("labelElement (tooltip security)", () => {
  // A minimal document: records what the function does to the element, and
  // fails loudly if anything reaches for an HTML sink.
  function fakeDoc() {
    const made: Record<string, unknown>[] = [];
    const doc = {
      createElement(tag: string) {
        const el: Record<string, unknown> = { tagName: tag, className: "", textContent: "" };
        for (const sink of ["innerHTML", "outerHTML"]) {
          Object.defineProperty(el, sink, {
            set() {
              throw new Error(`${sink} was written`);
            },
          });
        }
        el.insertAdjacentHTML = () => {
          throw new Error("insertAdjacentHTML was called");
        };
        made.push(el);
        return el;
      },
    };
    return { doc: doc as unknown as Document, made };
  }

  it("sets an agent-written name as text, never as markup", () => {
    const { doc, made } = fakeDoc();
    const payload = "<img src=x onerror=alert(document.domain)>";
    const el = labelElement(doc, payload) as unknown as Record<string, unknown>;
    expect(made).toHaveLength(1);
    expect(el.tagName).toBe("span");
    expect(el.textContent).toBe(payload);
  });

  it("builds a relation tooltip from the names, as plain text", () => {
    expect(linkLabelText({ source: { id: "a<b>" }, target: "c", relation: "uses" })).toBe("a<b> uses c");
    const { doc } = fakeDoc();
    const el = labelElement(doc, linkLabelText({ source: "x", target: "y", relation: "r" })) as unknown as Record<string, unknown>;
    expect(el.textContent).toBe("x r y");
  });
});

describe("hue selection", () => {
  it("hashes the first project deterministically and leaves unattributed entities without a hue", () => {
    const h = projectHue(["pseudolife-mcp", "other"]);
    expect(h).toBe(projectHue(["pseudolife-mcp"]));
    expect(h).toBeGreaterThanOrEqual(0);
    expect(h).toBeLessThan(360);
    expect(projectHue(["a"])).not.toBe(projectHue(["b"]));
    expect(projectHue([])).toBeNull();
    expect(projectHue(undefined)).toBeNull();
  });

  it("takes community hues from the brand palette and ignores missing or non-numeric ids", () => {
    expect(communityHue(0)).toBe(GALAXY_HUES[0]);
    expect(communityHue(1)).toBe(GALAXY_HUES[1]);
    expect(communityHue(-2)).toBe(GALAXY_HUES[2]);
    expect(communityHue("8")).toBe(GALAXY_HUES[8 % GALAXY_HUES.length]);
    expect(communityHue(null)).toBeNull();
    expect(communityHue("")).toBeNull();
    expect(communityHue("x")).toBeNull();
  });

  it("hues by project for all projects, else by community with a type fallback", () => {
    const nodes = [
      { entity: "a", sources: ["p1"], community: 1, etype: "service" },
      { entity: "b", sources: [], community: null, etype: "database" },
    ];
    const byProject = buildColors(nodes, [], "project");
    expect(byProject.get("a")?.h).toBe(projectHue(["p1"]));
    expect(byProject.get("b")).toEqual(UNATTRIBUTED);
    const byCommunity = buildColors(nodes, [], "community");
    expect(byCommunity.get("a")?.h).toBe(GALAXY_HUES[1]);
    expect(byCommunity.get("b")).toEqual(etypeHsl("database"));
  });

  it("falls back to the lavender family for an unknown type", () => {
    expect(etypeHsl("spaceship")).toEqual(etypeHsl("service"));
    expect(etypeHsl(null)).toEqual(etypeHsl("service"));
  });
});

describe("recency lightness", () => {
  it("runs 38% for the oldest to 62% for the newest", () => {
    expect(recencyLightness(100, 100, 200)).toBe(38);
    expect(recencyLightness(200, 100, 200)).toBe(62);
    expect(recencyLightness(150, 100, 200)).toBe(50);
    expect(recencyLightness(null, 100, 200)).toBe(38);
  });

  it("uses the later of creation and the newest edge", () => {
    const nodes = [
      { entity: "old", created_at: 100, community: 1 },
      { entity: "revived", created_at: 100, community: 1 },
      { entity: "new", created_at: 200, community: 1 },
    ];
    const colors = buildColors(nodes, [{ src: "revived", dst: "new", asserted_at: 200 }], "community");
    expect(colors.get("old")?.l).toBe(38);
    expect(colors.get("revived")?.l).toBe(62);
    expect(colors.get("new")?.l).toBe(62);
  });
});

describe("hslCss", () => {
  it("writes the comma syntax both renderers parse", () => {
    expect(hslCss({ h: 42, s: 85, l: 62 })).toBe("hsl(42, 85%, 62%)");
    expect(hslCss({ h: 0, s: 0, l: 50 }, 0.25)).toBe("hsla(0, 0%, 50%, 0.25)");
  });
});
