"""Source-level guard over the Console's vendored-3D-graph label sink.

No JS-executing harness exists in this repo (pure Python test suite, no
node_modules / jsdom / playwright) — see galaxy.js's history: the whole
static/js tree had zero test coverage before this file. That's how issue
#171 shipped: `.nodeLabel((n) => n.id)` in galaxy.js hands the vendored
3d-force-graph bundle a raw STRING label; the bundle's tooltip module
(`pseudolife_memory/web/static/vendor/galaxy.bundle.js`, the float-tooltip
"update" branch) calls `.html(content)` — i.e. innerHTML — on any string
content, so an attacker-controlled entity name (e.g. one containing an
`<img onerror=...>` payload, plausible via a prompt-injected ingested
document reaching the extractor) executes on hover. The bundle's *same*
tooltip update function appends an `HTMLElement` content value via
`selection.append(() => content)` instead — d3 inserts the live node
directly, never through innerHTML — so returning a DOM node (textContent
set) from the label accessor is the inert form.

These tests pin that contract at the source level: they fail loudly (RED)
if `nodeLabel`/`linkLabel` in galaxy.js goes back to returning a bare
interpolated string.
"""

from __future__ import annotations

import re
from pathlib import Path

from pseudolife_memory.web.fixtures import FixtureService

GALAXY_JS = (
    Path(__file__).resolve().parent.parent
    / "pseudolife_memory" / "web" / "static" / "js" / "galaxy.js"
)
STYLES_CSS = (
    Path(__file__).resolve().parent.parent
    / "pseudolife_memory" / "web" / "static" / "css" / "styles.css"
)
STATIC_JS_DIR = GALAXY_JS.parent


def _extract_call_arg(source: str, call_name: str) -> str:
    """Return the raw text of the single argument passed to `.call_name(...)`,
    matching parens so nested calls (e.g. `el("span", {}, n.id)`) don't
    truncate the extraction early."""
    marker = f".{call_name}("
    start = source.index(marker) + len(marker)
    depth = 1
    i = start
    while depth:
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
        i += 1
    return source[start : i - 1]


def test_galaxy_node_label_returns_dom_node_not_raw_string():
    """The vendored bundle's tooltip sink treats a string label as innerHTML
    (see module docstring). `.nodeLabel(...)` must therefore never resolve
    to a bare string built from node data — it must return an HTMLElement
    whose text is set via a text-node path (`el(...)`'s appendKids uses
    `document.createTextNode`, never `.innerHTML`)."""
    src = GALAXY_JS.read_text(encoding="utf-8")
    assert ".nodeLabel(" in src, "galaxy.js no longer sets a node label — update this pin"

    arg = _extract_call_arg(src, "nodeLabel")

    # The exact vulnerable shape from issue #171 must never come back.
    assert arg.strip() != "(n) => n.id", (
        "nodeLabel regressed to returning the raw entity-name string — this "
        "reaches the vendored tooltip's innerHTML sink (galaxy.bundle.js's "
        "float-tooltip `.html(content)` branch). Return a DOM node instead."
    )

    # The fixed shape must build an actual element (safe `.append(() =>
    # element)` branch in the vendored tooltip), not a template string.
    assert "el(" in arg or "document.createElement" in arg, (
        f"nodeLabel callback ({arg!r}) does not appear to construct a DOM "
        "element — it must return an HTMLElement, not a string, to avoid "
        "the tooltip's innerHTML sink."
    )
    assert "`" not in arg, (
        "nodeLabel callback uses a template literal — that's a string, "
        "which the tooltip renders via innerHTML. Return a DOM element."
    )
    # el()'s `html:` prop is an innerHTML escape hatch (util.js) — building
    # the element with it would reintroduce the sink while satisfying the
    # asserts above.
    assert "html" not in arg, (
        "nodeLabel callback passes an html: prop to el() — that path sets "
        ".innerHTML (util.js) and reopens the stored-XSS sink. Set the name "
        "as text content instead."
    )


def test_galaxy_link_label_not_set_to_raw_string():
    """Same sink applies to link tooltips (`.linkLabel(...)`) via the
    vendored bundle's shared `tooltipContent` dispatch keyed on
    `__graphObjType`. galaxy.js doesn't currently set linkLabel (the
    default `"name"` property accessor reads a field links don't carry,
    so it degrades to empty content) — this pin catches anyone adding one
    with the same string-interpolation mistake as the old nodeLabel."""
    src = GALAXY_JS.read_text(encoding="utf-8")
    if ".linkLabel(" not in src:
        return
    arg = _extract_call_arg(src, "linkLabel")
    assert "el(" in arg or "document.createElement" in arg, (
        f"linkLabel callback ({arg!r}) must return a DOM element, not a "
        "string, for the same reason as nodeLabel above."
    )
    assert "`" not in arg


def test_galaxy_js_has_no_other_bare_label_string_interpolation():
    """Belt-and-braces: no accessor named like a label/tooltip callback in
    galaxy.js should build its return value via string concatenation or
    template-literal interpolation of node/link data — the one documented
    safe shape is a DOM element built through `el(...)`."""
    src = GALAXY_JS.read_text(encoding="utf-8")
    for m in re.finditer(r"\.(node|link)(Label)\(", src):
        call = m.group(1) + m.group(2)
        arg = _extract_call_arg(src, call)
        assert "${" not in arg, f".{call}(...) interpolates into a template literal: {arg!r}"


def test_devserver_fixture_bank_carries_markup_shaped_entity_name():
    """The Console dev server (``pseudolife_memory.web.devserver``, backed by
    ``FixtureService``) is how a human eyeballs a live galaxy view without
    Postgres. Keep a markup-shaped entity name (an `<img onerror=...>`
    payload, the #171 shape) wired into the default demo graph so a
    maintainer running the dev server can hover it and see inert text —
    this pins that the fixture doesn't silently get dropped in a future
    edit of graph_neighborhood()."""
    svc = FixtureService()
    out = svc.graph_neighborhood(None, scope="all")
    names = {n["entity"] for n in out["nodes"]}
    payload = "<img src=x onerror=alert(document.domain)>"
    assert payload in names, (
        "the #171 XSS-probe entity is missing from the demo graph fixture — "
        "restore it in fixtures.py's graph_neighborhood() so the galaxy "
        "view fix stays eyeball-checkable via the dev server."
    )
    # And it must be reachable (not an orphaned node off the visible graph).
    edges = out["edges"]
    assert any(payload in (e["src"], e["dst"]) for e in edges), (
        "the XSS-probe entity has no edge — it would render off-screen and "
        "never come up in a normal dev-server eyeball check."
    )


def test_all_green_status_dots_are_static():
    """Healthy state chips must stay static on every Console view.

    Observatory renders its Postgres health chip outside ``.topbar-status``,
    so a topbar-only override leaves the same Firefox box-shadow compositing
    flicker active on that view.  Green state is stable; warning activity may
    continue to use the pulse treatment.
    """
    src = STYLES_CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.chip\.ok\s+\.pulse-dot\s*\{([^}]+)\}", src)
    assert rule, "all green health chips need a Firefox-safe static dot"
    body = rule.group(1).replace(" ", "")
    assert "animation:none" in body
    assert "opacity:1" in body


def test_reduced_motion_does_not_repeat_shortened_animations_forever():
    src = STYLES_CSS.read_text(encoding="utf-8")
    reduced = src[src.index("@media (prefers-reduced-motion:reduce)"):]
    rule = reduced[:reduced.index("}")]
    assert "animation-iteration-count:1 !important" in rule, (
        "shortening an infinite animation to .001ms creates rapid flicker; "
        "reduced motion must also limit its iteration count")


def test_activity_pulse_avoids_firefox_repaint_flicker():
    """Activity remains visible without animating a painted shadow.

    Firefox-family browsers can rapidly flicker the chip layer when the
    expanding ``box-shadow`` is repainted.  Opacity is composited instead of
    painted, so it preserves a restrained pulse without disturbing adjacent
    status chips.
    """
    src = STYLES_CSS.read_text(encoding="utf-8")
    pulse_rule = re.search(r"\.pulse-dot\s*\{([^}]+)\}", src)
    assert pulse_rule, "activity dots need a base pulse rule"
    body = pulse_rule.group(1).replace(" ", "")
    assert "animation:pulse" in body, "warning/activity dots must still pulse"
    assert "box-shadow" not in body

    keyframes = src[src.index("@keyframes pulse") : src.index("/*", src.index("@keyframes pulse"))]
    assert "opacity:" in keyframes
    assert "box-shadow" not in keyframes


def test_every_console_pulse_dot_has_an_explicit_status_kind():
    """No page may bypass the healthy/warning pulse policy."""
    offenders = []
    for path in STATIC_JS_DIR.rglob("*.js"):
        if "vendor" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if 'class: "pulse-dot"' in line and not re.search(
                r'class:\s*"chip\s+(?:ok|warn|bad)"', line
            ):
                offenders.append(f"{path.relative_to(STATIC_JS_DIR)}:{lineno}")
    assert not offenders, "unclassified pulse-dot use(s): " + ", ".join(offenders)


def test_graph_replay_is_not_a_silent_noop_under_reduced_motion():
    """A visible enabled play button must respond when explicitly invoked.

    Reduced motion already disables graph simulation/camera animation.  The
    time scrubber itself changes visibility without a motion transition, so a
    user-initiated replay remains safe and must not silently return.
    """
    src = GALAXY_JS.read_text(encoding="utf-8")
    play = src.index('class: "scrub-play"')
    handler = src[play : play + 500]
    assert "if (reduce) return" not in handler, (
        "Graph replay silently ignores clicks when Firefox reports reduced "
        "motion; keep the explicit replay operable."
    )
