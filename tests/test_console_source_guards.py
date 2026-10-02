"""Source-level guards over the Cortex Console's frontend (``frontend/src``).

The console's behaviour is unit-tested with vitest in CI's ``frontend`` job;
these pins live in the Python suite because they guard security contracts
that must fail every lane, not just the Node one.

The 3D galaxy's label sink (#171): the vendored 3d-force-graph bundle's
tooltip (``frontend/public/vendor/galaxy.bundle.js``, the float-tooltip
"update" branch) calls ``.html(content)``, that is innerHTML, on any STRING
label, so an attacker-controlled entity name (an ``<img onerror=...>``
payload, plausible via a prompt-injected ingested document reaching the
extractor) would execute on hover. The same function appends an
``HTMLElement`` content value via ``selection.append(() => content)``, which
never goes through innerHTML, so the label accessors must return a DOM node
whose text is set with ``textContent``.
"""

from __future__ import annotations

import re
from pathlib import Path

from pseudolife_memory.web.fixtures import FixtureService

SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"
GALAXY_TS = SRC / "lib" / "galaxy.ts"


def _sources() -> list[Path]:
    return [p for p in SRC.rglob("*") if p.suffix in {".svelte", ".ts"} and not p.name.endswith(".test.ts")]


def _extract_call_arg(source: str, call_name: str) -> str:
    """The raw text of the argument passed to ``.call_name(...)``, matching
    parentheses so nested calls do not truncate it."""
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


def test_galaxy_labels_are_dom_nodes_not_strings():
    src = GALAXY_TS.read_text(encoding="utf-8")
    for call in ("nodeLabel", "linkLabel"):
        assert f".{call}(" in src, f"galaxy.ts no longer sets {call}: update this pin"
    # Every label or tooltip accessor handed to the engine, anywhere in the
    # console, not just the first one in galaxy.ts.
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\.(node|link)(Label)\(", text):
            arg = _extract_call_arg(text[m.start():], m.group(1) + m.group(2))
            assert "labelElement(" in arg, (
                f"{path.relative_to(SRC)}: .{m.group(1)}Label must return labelElement(...), "
                "a DOM node, never a string: the vendored tooltip renders strings via innerHTML (#171)"
            )
            assert "`" not in arg and "${" not in arg, f"{path.relative_to(SRC)} builds a template string: {arg!r}"


def test_label_element_sets_text_content_only():
    src = GALAXY_TS.read_text(encoding="utf-8")
    m = re.search(r"export function labelElement\([^)]*\)[^{]*\{(.*?)\n\}", src, re.S)
    assert m, "labelElement() is missing from galaxy.ts"
    body = m.group(1)
    assert ".textContent" in body, "labelElement must set textContent"
    assert "innerHTML" not in body and "outerHTML" not in body and "insertAdjacentHTML" not in body


def test_no_html_sinks_anywhere_in_the_console():
    """Text from the bank renders as text. ``{@html}`` and direct innerHTML
    assignment are the two ways a Svelte app reopens an XSS sink."""
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        assert "{@html" not in text, f"{path.relative_to(SRC)} uses {{@html}}"
        assert not re.search(r"\.(innerHTML|outerHTML)\s*=", text), f"{path.relative_to(SRC)} assigns HTML"
        assert "insertAdjacentHTML" not in text, f"{path.relative_to(SRC)} inserts HTML"


# Data-driven hrefs that are not built by hrefTo()/hrefFor() (in-app hash
# routes, URL-encoded). Each is reviewed: SourceLink's is safeHttpUrl's
# result; the other two are built from hrefTo() or literal "#/..." routes.
_REVIEWED_HREFS = {
    ("SourceLink.svelte", "safe"),
    ("Graph.svelte", "reviewHref"),
    ("Observatory.svelte", "s.href"),
}


def test_every_data_driven_href_is_an_app_route_or_checked():
    """An agent- or model-written URL must never reach an href unchecked. In
    the console every dynamic href is an in-app route built by hrefTo() /
    hrefFor(), or one of the reviewed expressions above; a new one fails
    here until someone looks at it."""
    safe = (SRC / "lib" / "safe.ts").read_text(encoding="utf-8")
    assert "^https?:\\/\\/" in safe
    for path in SRC.rglob("*.svelte"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"href=\{([^}]*)\}", text):
            expr = m.group(1).strip()
            if "hrefTo(" in expr or "hrefFor(" in expr:
                continue
            assert (path.name, expr) in _REVIEWED_HREFS, (
                f"{path.relative_to(SRC)} links href={{{expr}}}: build in-app links with "
                "hrefTo(), and external ones only through SourceLink (safeHttpUrl)"
            )
    link = (SRC / "components" / "SourceLink.svelte").read_text(encoding="utf-8")
    assert "safeHttpUrl(url)" in link and "href={safe}" in link


def test_only_source_link_opens_external_pages():
    for path in SRC.rglob("*.svelte"):
        if path.name == "SourceLink.svelte":
            continue
        assert 'target="_blank"' not in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(SRC)} opens an external page: route it through SourceLink"
        )


def test_devserver_fixture_bank_carries_markup_shaped_entity_name():
    """The fixture devserver is how a human eyeballs the galaxy without
    Postgres. Keep a markup-shaped entity name (the #171 ``<img onerror>``
    shape) in the demo graph, connected, so hovering it shows inert text."""
    svc = FixtureService()
    out = svc.graph_neighborhood(None, scope="all")
    names = {n["entity"] for n in out["nodes"]}
    payload = "<img src=x onerror=alert(document.domain)>"
    assert payload in names, (
        "the #171 XSS-probe entity is missing from the demo graph fixture: "
        "restore it in fixtures.py's graph_neighborhood()"
    )
    assert any(payload in (e["src"], e["dst"]) for e in out["edges"]), (
        "the XSS-probe entity has no edge, so it would never come up in a "
        "normal dev-server check"
    )
