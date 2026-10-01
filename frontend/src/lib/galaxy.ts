// The 3D galaxy engine: wraps the vendored bundle (3d-force-graph + its own
// three.js, one instance) loaded lazily from <base>vendor/galaxy.bundle.js.
// Encodings: size = connections + facts, hue = project (scope "all") or
// community, lightness = recency of the entity's last activity. Community
// nebulae with constellation names, labels on the nearest stars only, search
// highlight, isolate, pulsing flagged stars, a visibility-only time cut and a
// camera that stops moving on its own once the person touches it.
//
// Imperative on purpose: a Svelte component owns the host element and calls
// createGalaxy() / handle.destroy(); nothing here imports Svelte.

import { neighborhood, nodeSize } from "./graph";
import type { GraphEdge, GraphNode, GraphResponse } from "./api/graph";

// ---- colour encodings (pure) ------------------------------------------------

export interface Hsl {
  h: number;
  s: number;
  l: number;
}

export type ColorBy = "project" | "community";

/** An entity no project owns, when hue means project. */
export const UNATTRIBUTED: Hsl = { h: 250, s: 6, l: 50 };
/** Stars outside the search match or the isolated neighbourhood. */
const DIM: Hsl = { h: 260, s: 8, l: 42 };
/** Search hits. */
const HIT: Hsl = { h: 0, s: 0, l: 100 };
/** Flagged stars: the warning orange (a static tint under reduced motion). */
export const FLAG: Hsl = { h: 27, s: 96, l: 61 };

/**
 * Fallback colours by entity type when an entity has no community: the
 * console's lavender (associative) and gold (canonical) family.
 */
const ETYPE: Record<string, Hsl> = {
  service: { h: 273, s: 70, l: 76 },
  database: { h: 42, s: 85, l: 62 },
  host: { h: 266, s: 58, l: 68 },
  model: { h: 36, s: 88, l: 64 },
  person: { h: 300, s: 42, l: 74 },
  concept: { h: 45, s: 78, l: 72 },
};
const ETYPE_DEFAULT: Hsl = ETYPE.service;

export function etypeHsl(etype: string | null | undefined): Hsl {
  return (etype && ETYPE[etype]) || ETYPE_DEFAULT;
}

/** CSS colour, comma syntax (both three.js and the bundle's parser read it). */
export function hslCss(c: Hsl, alpha = 1): string {
  const body = `${Math.round(c.h)}, ${Math.round(c.s)}%, ${Math.round(c.l)}%`;
  return alpha >= 1 ? `hsl(${body})` : `hsla(${body}, ${Math.round(alpha * 1000) / 1000})`;
}

/** Deterministic hue from the entity's first project; null when unattributed. */
export function projectHue(sources: readonly string[] | null | undefined): number | null {
  const s = sources?.[0] ?? "";
  if (!s) return null;
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}

/** Hue from a community id (47 degrees apart); null when there is none. */
export function communityHue(community: number | string | null | undefined): number | null {
  if (community === null || community === undefined || community === "") return null;
  const n = Number(community);
  if (!Number.isFinite(n)) return null;
  return (Math.abs(n) * 47) % 360;
}

/** Lightness 38%..62%: the newest activity is the brightest. */
export function recencyLightness(t: number | null | undefined, lo: number, hi: number): number {
  const span = hi > lo ? hi - lo : 1;
  const x = ((t || lo) - lo) / span;
  return Math.round(38 + Math.max(0, Math.min(1, x)) * 24);
}

/**
 * Each entity's base colour. Activity is the later of its creation and its
 * newest edge; the lightness range spans the whole graph's activity.
 */
export function buildColors(
  nodes: readonly Pick<GraphNode, "entity" | "etype" | "community" | "sources" | "created_at">[],
  edges: readonly Pick<GraphEdge, "src" | "dst" | "asserted_at">[],
  colorBy: ColorBy,
): Map<string, Hsl> {
  const act = new Map<string, number>();
  for (const n of nodes) act.set(n.entity, n.created_at || 0);
  for (const e of edges) {
    const t = e.asserted_at || 0;
    act.set(e.src, Math.max(act.get(e.src) || 0, t));
    act.set(e.dst, Math.max(act.get(e.dst) || 0, t));
  }
  let lo = Infinity;
  let hi = -Infinity;
  for (const t of act.values()) {
    if (!t) continue;
    if (t < lo) lo = t;
    if (t > hi) hi = t;
  }
  if (lo === Infinity) {
    lo = 0;
    hi = 1;
  }
  const out = new Map<string, Hsl>();
  for (const n of nodes) {
    const h = colorBy === "project" ? projectHue(n.sources) : communityHue(n.community);
    if (h === null) {
      out.set(n.entity, colorBy === "project" ? UNATTRIBUTED : etypeHsl(n.etype));
    } else {
      out.set(n.entity, { h, s: 64, l: recencyLightness(act.get(n.entity), lo, hi) });
    }
  }
  return out;
}

/** A few sample hues for the legend swatch. */
export const LEGEND_HUES: readonly Hsl[] = [0, 1, 2, 3, 4].map((i) => ({ h: (i * 47 * 3) % 360, s: 64, l: 58 }));

// ---- tooltips (security) ----------------------------------------------------

/**
 * A tooltip for a star or a relation. Entity names are agent-written and the
 * vendored float-tooltip assigns a STRING label to innerHTML (classic #171),
 * so a label is always an element whose text is set with textContent; an
 * element takes the tooltip's append path, never the HTML one.
 */
export function labelElement(doc: Pick<Document, "createElement">, text: string): HTMLElement {
  const span = doc.createElement("span");
  span.className = "galaxy-tip";
  span.textContent = text;
  return span;
}

/** Plain text for a relation tooltip: "src relation dst". */
export function linkLabelText(l: { source: unknown; target: unknown; relation: string }): string {
  return [endName(l.source), l.relation, endName(l.target)].join(" ");
}

function endName(v: unknown): string {
  if (typeof v === "string") return v;
  if (v && typeof v === "object" && "id" in v) return String((v as { id: unknown }).id);
  return "";
}

// ---- minimal types for the vendored bundle ----------------------------------

interface V3 {
  x: number;
  y: number;
  z: number;
  copy(v: V3): V3;
  set(x: number, y: number, z: number): V3;
  setScalar(s: number): V3;
}

interface Obj3D {
  position: V3;
  scale: V3;
  visible: boolean;
  add(...o: Obj3D[]): void;
  clear(): void;
  children: Obj3D[];
}

interface Texture {
  colorSpace: string;
  dispose(): void;
}

interface SpriteMaterial {
  map: Texture | null;
  color: { setStyle(css: string): void };
  opacity: number;
  dispose(): void;
}

interface Sprite extends Obj3D {
  material: SpriteMaterial;
  center: { set(x: number, y: number): void };
  raycast: (...args: unknown[]) => void;
}

interface ThreeNS {
  CanvasTexture: new (canvas: HTMLCanvasElement) => Texture;
  SpriteMaterial: new (params: Record<string, unknown>) => SpriteMaterial;
  Sprite: new (material: SpriteMaterial) => Sprite;
  Group: new () => Obj3D;
  Vector3: new () => V3;
  AdditiveBlending: number;
  SRGBColorSpace: string;
}

interface Renderer {
  dispose(): void;
  forceContextLoss(): void;
  domElement: HTMLCanvasElement;
}

type Chain = (...args: unknown[]) => FG;

interface FG {
  graphData(): { nodes: GNode[]; links: GLink[] };
  graphData(d: { nodes: GNode[]; links: GLink[] }): FG;
  backgroundColor: Chain;
  nodeId: Chain;
  nodeLabel: Chain;
  linkLabel: Chain;
  nodeColor: Chain;
  nodeVal: Chain;
  nodeThreeObjectExtend: Chain;
  nodeThreeObject: Chain;
  nodeVisibility: Chain;
  linkVisibility: Chain;
  linkColor: Chain;
  linkDirectionalArrowLength: Chain;
  width: Chain;
  height: Chain;
  showNavInfo: Chain;
  warmupTicks: Chain;
  cooldownTime: Chain;
  cooldownTicks: Chain;
  onNodeClick: Chain;
  onEngineStop: Chain;
  zoomToFit: Chain;
  cameraPosition: Chain;
  pauseAnimation: Chain;
  camera(): { position: V3 };
  scene(): Obj3D;
  renderer(): Renderer;
  controls(): { dispose?: () => void } | null | undefined;
  _destructor?: () => void;
}

type FGCtor = new (el: HTMLElement, config?: Record<string, unknown>) => FG;

interface GNode {
  id: string;
  etype?: string | null;
  community?: number | string | null;
  created_at?: number | null;
  x?: number;
  y?: number;
  z?: number;
  __threeObj?: Obj3D;
  __group?: Obj3D;
  __glow?: Sprite;
  __label?: Sprite;
}

interface GLink {
  source: string | GNode;
  target: string | GNode;
  relation: string;
  derived: boolean;
  asserted_at?: number | null;
}

interface Engine {
  FG: FGCtor;
  THREE: ThreeNS;
}

let enginePromise: Promise<Engine> | null = null;

/** Load the vendored bundle once per page; a failure is retried next time. */
function loadEngine(): Promise<Engine> {
  if (!enginePromise) {
    const url = `${import.meta.env.BASE_URL}vendor/galaxy.bundle.js`;
    enginePromise = import(/* @vite-ignore */ url)
      .then((mod: { default?: unknown; THREE?: unknown }) => {
        if (typeof mod.default !== "function" || !mod.THREE) throw new Error("galaxy bundle exports missing");
        return { FG: mod.default as FGCtor, THREE: mod.THREE as ThreeNS };
      })
      .catch((e: unknown) => {
        enginePromise = null;
        throw e;
      });
  }
  return enginePromise;
}

// ---- the galaxy ---------------------------------------------------------------

/** How many of the nearest stars carry a visible name. */
const LABEL_NEAREST = 40;
/** The largest N communities (of at least 3 stars) get a nebula. */
const NEBULA_MAX = 12;
const LABEL_INK = "#ece6f5";
const GLOW_OPACITY = 0.32;

export interface GalaxyOptions {
  colorBy: ColorBy;
  reduceMotion: boolean;
  onNodeClick: (name: string) => void;
}

export interface GalaxyHandle {
  /** Fly the camera to a star; freezes the layout. False when it is not placed. */
  flyTo(name: string): boolean;
  /** Live search highlight (matches white, the rest dim). */
  setQuery(q: string): void;
  /** Stars that pulse (or carry a static tint under reduced motion). */
  setFlagged(names: ReadonlySet<string>): void;
  /** Light the neighbourhood within `depth` hops of `name` and dim the rest. */
  isolate(name: string, depth?: number): boolean;
  clearIsolate(): void;
  setHideOrphans(on: boolean): void;
  /** Hide everything newer than `t` (epoch seconds); null shows all. Never re-simulates. */
  setTimeCut(t: number | null): void;
  destroy(): void;
}

function makeCanvas(w: number, h: number): HTMLCanvasElement {
  const cv = document.createElement("canvas");
  cv.width = w;
  cv.height = h;
  return cv;
}

const SPRITE_FONT = 'Geist, "Segoe UI", system-ui, sans-serif';
const SPRITE_DPR = 2;

function textSprite(T: ThreeNS, text: string, color: string, px: number): Sprite {
  const shown = text.length > 48 ? `${text.slice(0, 46)}…` : text;
  const font = `500 ${px * SPRITE_DPR}px ${SPRITE_FONT}`;
  const meas = makeCanvas(1, 1).getContext("2d");
  if (meas) meas.font = font;
  const pad = 10 * SPRITE_DPR;
  const w = Math.ceil((meas ? meas.measureText(shown).width : shown.length * px * 1.2) + pad * 2);
  const h = (px + 18) * SPRITE_DPR;
  const cv = makeCanvas(w, h);
  const c = cv.getContext("2d");
  if (c) {
    c.font = font;
    c.textAlign = "center";
    c.textBaseline = "middle";
    c.shadowColor = "rgba(0, 0, 0, 0.75)";
    c.shadowBlur = 8 * SPRITE_DPR;
    c.fillStyle = color;
    c.fillText(shown, w / 2, h / 2);
  }
  const tex = new T.CanvasTexture(cv);
  tex.colorSpace = T.SRGBColorSpace;
  const sp = new T.Sprite(new T.SpriteMaterial({ map: tex, depthWrite: false, transparent: true }));
  sp.raycast = noRaycast;
  const scale = px * 0.32;
  sp.scale.set(scale * (w / h), scale, 1);
  return sp;
}

function glowTexture(T: ThreeNS): Texture {
  const cv = makeCanvas(64, 64);
  const c = cv.getContext("2d");
  if (c) {
    const g = c.createRadialGradient(32, 32, 0, 32, 32, 32);
    g.addColorStop(0, "rgba(255, 255, 255, 1)");
    g.addColorStop(0.18, "rgba(255, 255, 255, 0.55)");
    g.addColorStop(0.5, "rgba(255, 255, 255, 0.12)");
    g.addColorStop(1, "rgba(255, 255, 255, 0)");
    c.fillStyle = g;
    c.fillRect(0, 0, 64, 64);
  }
  const tex = new T.CanvasTexture(cv);
  tex.colorSpace = T.SRGBColorSpace;
  return tex;
}

function nebulaSprite(T: ThreeNS, hue: number): Sprite {
  const cv = makeCanvas(256, 256);
  const c = cv.getContext("2d");
  if (c) {
    const g = c.createRadialGradient(128, 128, 8, 128, 128, 128);
    g.addColorStop(0, `hsla(${hue}, 70%, 62%, 0.16)`);
    g.addColorStop(0.55, `hsla(${hue}, 70%, 55%, 0.07)`);
    g.addColorStop(1, `hsla(${hue}, 70%, 50%, 0)`);
    c.fillStyle = g;
    c.fillRect(0, 0, 256, 256);
  }
  const tex = new T.CanvasTexture(cv);
  tex.colorSpace = T.SRGBColorSpace;
  return new T.Sprite(new T.SpriteMaterial({ map: tex, depthWrite: false, transparent: true }));
}

function disposeSprite(s: Sprite | undefined): void {
  if (!s) return;
  s.material.map?.dispose();
  s.material.dispose();
}

const idOf = (v: string | GNode): string => (typeof v === "string" ? v : v.id);

/**
 * Decorations (halos, names) must not take hover or clicks: the engine
 * raycasts a star's children recursively and ignores `visible`, so a wide
 * halo or a hidden label would steal the pointer from the star behind it.
 */
function noRaycast(): void {}

/**
 * Stop the engine and free what its own destructor leaves behind: it only
 * pauses the loop and empties the data, so the controls (window key
 * listeners) and the renderer are released here. The WebGL context is lost
 * now rather than whenever GC gets to it: browsers cap live contexts, and
 * leaving the route must not leak one.
 */
function releaseEngine(fg: FG): void {
  let renderer: Renderer | null = null;
  try {
    renderer = fg.renderer();
  } catch {
    renderer = null;
  }
  try {
    fg.pauseAnimation();
    fg._destructor?.();
  } catch {
    /* best effort */
  }
  try {
    fg.controls()?.dispose?.();
  } catch {
    /* best effort */
  }
  try {
    renderer?.dispose();
    renderer?.forceContextLoss();
  } catch {
    /* already lost */
  }
}

/** Wait (briefly) for Geist so the canvas labels are not drawn in a fallback face. */
async function fontsReady(): Promise<void> {
  const fonts = (document as Document & { fonts?: FontFaceSet }).fonts;
  if (!fonts?.load) return;
  await Promise.race([
    fonts.load(`500 26px ${SPRITE_FONT}`).catch(() => undefined),
    new Promise((r) => setTimeout(r, 800)),
  ]);
}

/**
 * Build the galaxy inside `host`. Resolves null when the bundle or WebGL is
 * unavailable, or when the host left the document while the bundle loaded;
 * the caller then falls back to the table.
 */
export async function createGalaxy(host: HTMLElement, data: GraphResponse, opts: GalaxyOptions): Promise<GalaxyHandle | null> {
  let engine: Engine;
  try {
    engine = await loadEngine();
    await fontsReady();
  } catch (e) {
    console.error("galaxy bundle load failed", e);
    return null;
  }
  if (!host.isConnected) return null;
  const { FG, THREE: T } = engine;
  const reduce = opts.reduceMotion;

  // ---- data --------------------------------------------------------------
  const rawNodes = data.nodes ?? [];
  const names = new Set<string>();
  const nodes: GNode[] = [];
  const facts = new Map<string, number>();
  for (const n of rawNodes) {
    if (names.has(n.entity)) continue;
    names.add(n.entity);
    facts.set(n.entity, (n.facts ?? []).length);
    nodes.push({ id: n.entity, etype: n.etype, community: n.community, created_at: n.created_at });
  }
  // An edge to a node outside the payload would abort the layout.
  const rawEdges = (data.edges ?? []).filter((e) => names.has(e.src) && names.has(e.dst));
  const links: GLink[] = rawEdges.map((e) => ({
    source: e.src,
    target: e.dst,
    relation: e.relation,
    derived: !!e.derived,
    asserted_at: e.asserted_at,
  }));
  const deg = new Map<string, number>();
  for (const e of rawEdges) {
    deg.set(e.src, (deg.get(e.src) ?? 0) + 1);
    deg.set(e.dst, (deg.get(e.dst) ?? 0) + 1);
  }
  const colors = buildColors(rawNodes, rawEdges, opts.colorBy);
  const nodeVal = (n: GNode) => nodeSize(deg.get(n.id) ?? 0, facts.get(n.id) ?? 0);

  // ---- state layers --------------------------------------------------------
  // Priority: search query > isolate dim > flagged tint (reduced motion only;
  // motion users get the pulse) > base recency colour.
  const state = {
    query: "",
    dim: null as Set<string> | null,
    flagged: new Set<string>() as ReadonlySet<string>,
    tCut: null as number | null,
    hideOrphans: false,
  };

  function tone(n: GNode): { c: Hsl; a: number } {
    if (state.query) return n.id.toLowerCase().includes(state.query) ? { c: HIT, a: 1 } : { c: DIM, a: 0.25 };
    if (state.dim && !state.dim.has(n.id)) return { c: DIM, a: 0.16 };
    if (reduce && state.flagged.has(n.id)) return { c: FLAG, a: 1 };
    return { c: colors.get(n.id) ?? UNATTRIBUTED, a: 1 };
  }
  const nodeColor = (n: GNode) => {
    const t = tone(n);
    return hslCss(t.c, t.a);
  };
  // One definition per state-dependent accessor; poke() installs fresh thin
  // wrappers around these, never restated bodies (the classic hide-orphans
  // bug was a stale copy inside poke).
  const nodeVisible = (n: GNode) => {
    if (state.hideOrphans && !deg.get(n.id)) return false;
    return state.tCut === null || (n.created_at || 0) <= state.tCut;
  };
  const linkVisible = (l: GLink) => {
    if (state.tCut === null) return true;
    const s = typeof l.source === "object" ? l.source.created_at || 0 : 0;
    const t = typeof l.target === "object" ? l.target.created_at || 0 : 0;
    return (l.asserted_at || 0) <= state.tCut && s <= state.tCut && t <= state.tCut;
  };
  const linkColorOf = (l: GLink) => {
    const dimmed = state.dim && !(state.dim.has(idOf(l.source)) && state.dim.has(idOf(l.target)));
    if (dimmed) return "rgba(120, 115, 130, 0.06)";
    return l.derived ? "rgba(214, 200, 235, 0.20)" : "rgba(214, 200, 235, 0.42)";
  };
  function tintGlow(n: GNode) {
    const g = n.__glow;
    if (!g) return;
    const flaggedMotion = !reduce && state.flagged.has(n.id) && !state.query && !(state.dim && !state.dim.has(n.id));
    const t = flaggedMotion ? { c: FLAG, a: 1 } : tone(n);
    g.material.color.setStyle(hslCss(t.c));
    g.material.opacity = GLOW_OPACITY * t.a * t.a * (flaggedMotion ? 1.6 : 1);
  }

  // ---- engine ---------------------------------------------------------------
  // Stars currently carrying a visible name (see rankLabels).
  const labelled = new Set<GNode>();

  const mountPt = document.createElement("div");
  mountPt.className = "galaxy-mount";
  host.appendChild(mountPt);
  const glowTex = glowTexture(T);
  const box0 = host.getBoundingClientRect();

  let fg: FG;
  try {
    fg = new FG(mountPt, {});
    fg.graphData({ nodes, links });
  } catch (e) {
    // No WebGL (or a context the browser refused).
    console.error("galaxy WebGL init failed", e);
    glowTex.dispose();
    mountPt.remove();
    return null;
  }
  try {
    fg.backgroundColor("rgba(0,0,0,0)")
      .nodeId("id")
      // Security: an element with textContent, never a string (see labelElement).
      .nodeLabel((n: GNode) => labelElement(document, n.id))
      .linkLabel((l: GLink) => labelElement(document, linkLabelText(l)))
      .nodeColor((n: GNode) => nodeColor(n))
      .nodeVal(nodeVal)
      .nodeThreeObjectExtend(true)
      .nodeThreeObject((n: GNode) => {
        // Called again whenever a hidden star (time cut, hidden orphan) comes
        // back: the engine removed and disposed its old objects, label included.
        n.__label = undefined;
        labelled.delete(n);
        const group = new T.Group();
        const glow = new T.Sprite(
          new T.SpriteMaterial({
            map: glowTex,
            blending: T.AdditiveBlending,
            depthWrite: false,
            transparent: true,
            opacity: GLOW_OPACITY,
          }),
        );
        glow.raycast = noRaycast;
        const r = Math.cbrt(nodeVal(n)) * 4;
        glow.scale.set(r * 4.5, r * 4.5, 1);
        group.add(glow);
        n.__group = group;
        n.__glow = glow;
        tintGlow(n);
        return group;
      })
      .nodeVisibility((n: GNode) => nodeVisible(n))
      .linkVisibility((l: GLink) => linkVisible(l))
      .linkColor((l: GLink) => linkColorOf(l))
      .linkDirectionalArrowLength(3)
      .width(box0.width)
      .height(box0.height)
      .showNavInfo(false)
      // Pre-simulate before the first paint so an early fly-to (which freezes
      // the layout) lands on a mostly settled map, not a mid-explosion one.
      .warmupTicks(reduce ? 120 : 60)
      .cooldownTime(reduce ? 0 : 12000)
      .onNodeClick((n: GNode) => opts.onNodeClick(n.id));
  } catch (e) {
    console.error("galaxy configuration failed", e);
    releaseEngine(fg);
    glowTex.dispose();
    mountPt.remove();
    return null;
  }

  let destroyed = false;
  const timers: ReturnType<typeof setTimeout>[] = [];
  const later = (fn: () => void, ms: number) => timers.push(setTimeout(() => !destroyed && fn(), ms));

  // Fresh closures each time: the engine may treat the same function
  // reference as unchanged and skip the scene update.
  function poke() {
    fg.nodeColor((n: GNode) => nodeColor(n));
    fg.linkColor((l: GLink) => linkColorOf(l));
    fg.nodeVisibility((n: GNode) => nodeVisible(n));
    fg.linkVisibility((l: GLink) => linkVisible(l));
    for (const n of nodes) tintGlow(n);
  }

  // ---- camera policy ---------------------------------------------------------
  // The camera never moves on its own once the person has shown intent
  // (orbit, wheel or a fly-to). Auto-fit runs only on an untouched view: once
  // early (the layout has spread) and once when the engine settles.
  let interacted = false;
  const onTouch = () => {
    interacted = true;
  };
  host.addEventListener("pointerdown", onTouch, { capture: true });
  host.addEventListener("wheel", onTouch, { capture: true, passive: true });
  const fitCam = () => {
    try {
      fg.zoomToFit(reduce ? 0 : 400, 40);
    } catch {
      /* the scene may be empty */
    }
  };
  later(() => {
    if (!interacted) fitCam();
  }, 700);

  // ---- nebulae and constellations ----------------------------------------------
  const nebulae = new T.Group();
  fg.scene().add(nebulae);
  let nebulaSprites: Sprite[] = [];
  function paintNebulae() {
    for (const s of nebulaSprites) disposeSprite(s);
    nebulaSprites = [];
    nebulae.clear();
    const byComm = new Map<string, GNode[]>();
    for (const n of nodes) {
      if (n.community === null || n.community === undefined || n.x === undefined) continue;
      if (state.hideOrphans && !deg.get(n.id)) continue;
      const k = String(n.community);
      const list = byComm.get(k);
      if (list) list.push(n);
      else byComm.set(k, [n]);
    }
    const top = [...byComm.entries()]
      .sort((a, b) => b[1].length - a[1].length)
      .slice(0, NEBULA_MAX)
      .filter(([, m]) => m.length >= 3);
    for (const [cid, members] of top) {
      const hue = communityHue(cid);
      if (hue === null) continue;
      const cx = members.reduce((s, n) => s + (n.x ?? 0), 0) / members.length;
      const cy = members.reduce((s, n) => s + (n.y ?? 0), 0) / members.length;
      const cz = members.reduce((s, n) => s + (n.z ?? 0), 0) / members.length;
      const spread =
        Math.sqrt(
          members.reduce((s, n) => s + ((n.x ?? 0) - cx) ** 2 + ((n.y ?? 0) - cy) ** 2 + ((n.z ?? 0) - cz) ** 2, 0) /
            members.length,
        ) || 20;
      const cloud = nebulaSprite(T, hue);
      cloud.position.set(cx, cy, cz);
      cloud.scale.set(spread * 3.2, spread * 3.2, 1);
      const anchor = members.reduce((best, n) => ((deg.get(n.id) ?? 0) > (deg.get(best.id) ?? 0) ? n : best), members[0]);
      const label = textSprite(T, anchor.id, `hsl(${hue}, 70%, 72%)`, 34);
      label.position.set(cx, cy + spread * 1.5, cz);
      label.material.opacity = 0.75;
      nebulae.add(cloud, label);
      nebulaSprites.push(cloud, label);
    }
  }
  let fitted = false;
  // Every recolour restarts the engine for a frame, so it "stops" again on
  // each keystroke or scrubber step; repaint only when the stars moved.
  let layoutSig = NaN;
  const signature = () => nodes.reduce((s, n) => s + (n.x ?? 0) * 1.3 + (n.y ?? 0) * 1.7 + (n.z ?? 0), 0);
  fg.onEngineStop(() => {
    if (destroyed) return;
    if (!fitted) {
      fitted = true;
      if (!interacted) fitCam();
    }
    const sig = signature();
    if (sig === layoutSig) return;
    layoutSig = sig;
    paintNebulae();
  });
  later(paintNebulae, 1600);

  // ---- nearest-star labels and the pulse ----------------------------------------
  const camera = fg.camera();
  const camPos = new T.Vector3();
  let raf = 0;
  let lastRank = 0;

  function shown(n: GNode): boolean {
    if (n.x === undefined) return false;
    if (!nodeVisible(n)) return false;
    if (state.dim && !state.dim.has(n.id)) return false;
    return true;
  }
  function rankLabels() {
    camPos.copy(camera.position);
    const ranked: { n: GNode; d: number }[] = [];
    for (const n of nodes) {
      if (!shown(n)) continue;
      const dx = (n.x ?? 0) - camPos.x;
      const dy = (n.y ?? 0) - camPos.y;
      const dz = (n.z ?? 0) - camPos.z;
      ranked.push({ n, d: dx * dx + dy * dy + dz * dz });
    }
    ranked.sort((a, b) => a.d - b.d);
    const next = new Set<GNode>();
    for (let i = 0; i < ranked.length && i < LABEL_NEAREST; i++) next.add(ranked[i].n);
    for (const n of labelled) if (!next.has(n) && n.__label) n.__label.visible = false;
    for (const n of next) {
      if (!n.__label && n.__group) {
        // Labels are made on demand: a 2,000-star bank never draws 2,000 canvases.
        const sp = textSprite(T, n.id, LABEL_INK, 26);
        sp.center.set(0.5, -0.9);
        n.__group.add(sp);
        n.__label = sp;
      }
      if (n.__label) n.__label.visible = true;
    }
    labelled.clear();
    for (const n of next) labelled.add(n);
  }
  function frame(now: number) {
    if (destroyed) return;
    if (now - lastRank > 90) {
      lastRank = now;
      rankLabels();
    }
    if (!reduce && state.flagged.size) {
      const s = 1 + 0.22 * Math.sin(now / 300);
      for (const n of nodes) {
        const o = n.__threeObj;
        if (!o) continue;
        if (state.flagged.has(n.id)) o.scale.setScalar(s);
        else if (o.scale.x !== 1) o.scale.setScalar(1);
      }
    }
    raf = requestAnimationFrame(frame);
  }
  raf = requestAnimationFrame(frame);

  // ---- resize ---------------------------------------------------------------------
  const ro = new ResizeObserver(() => {
    const b = host.getBoundingClientRect();
    if (b.width && b.height) fg.width(b.width).height(b.height);
  });
  ro.observe(host);

  // ---- handle -----------------------------------------------------------------------
  function flyTo(name: string): boolean {
    if (destroyed) return false;
    const n = nodes.find((x) => x.id === name);
    if (!n || n.x === undefined) return false;
    interacted = true;
    // Freeze the layout: the inspected star must stay where the camera put it.
    try {
      fg.cooldownTicks(0);
    } catch {
      /* ignore */
    }
    const x = n.x ?? 0;
    const y = n.y ?? 0;
    const z = n.z ?? 0;
    const d = Math.hypot(x, y, z) || 1;
    const dist = 55 + nodeVal(n) * 2;
    const ratio = 1 + dist / d;
    fg.cameraPosition({ x: x * ratio, y: y * ratio, z: z * ratio }, { x, y, z }, reduce ? 0 : 1100);
    return true;
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    for (const t of timers) clearTimeout(t);
    cancelAnimationFrame(raf);
    ro.disconnect();
    host.removeEventListener("pointerdown", onTouch, { capture: true });
    host.removeEventListener("wheel", onTouch, { capture: true });
    for (const s of nebulaSprites) disposeSprite(s);
    for (const n of nodes) {
      disposeSprite(n.__label);
      n.__glow?.material.dispose();
    }
    glowTex.dispose();
    releaseEngine(fg);
    mountPt.remove();
  }

  return {
    flyTo,
    setQuery(q: string) {
      state.query = q.trim().toLowerCase();
      poke();
    },
    setFlagged(names: ReadonlySet<string>) {
      state.flagged = names;
      if (!reduce) for (const n of nodes) if (!names.has(n.id)) n.__threeObj?.scale.setScalar(1);
      poke();
    },
    isolate(name: string, depth = 2) {
      if (!names.has(name)) return false;
      const keep = neighborhood([...names], rawEdges, name, depth);
      if (!keep) return false;
      state.dim = keep;
      poke();
      return true;
    },
    clearIsolate() {
      if (!state.dim) return;
      state.dim = null;
      poke();
    },
    setHideOrphans(on: boolean) {
      if (state.hideOrphans === on) return;
      state.hideOrphans = on;
      poke();
      paintNebulae();
    },
    setTimeCut(t: number | null) {
      state.tCut = t;
      poke();
    },
    destroy,
  };
}
