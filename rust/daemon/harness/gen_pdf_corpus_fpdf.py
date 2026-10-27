"""Write the realistic half of ``pdf_corpus/``: PDFs laid out by a real
generator (fpdf2) with an embedded, subset TrueType font (DejaVu, under the
Bitstream Vera / DejaVu licence, which permits embedding) and its ToUnicode
map, justified and shaped text, columns, a table and several scripts.

``gen_pdf_corpus.py`` writes the hand-built half. This one needs fpdf2 and
uharfbuzz (text shaping: kerning and ligatures, written as ``TJ`` arrays)
and the DejaVu fonts, so it is run once on a machine that has them and its
output committed:

  python gen_pdf_corpus_fpdf.py --font-dir /usr/share/fonts/truetype/dejavu
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "pdf_corpus"

PARAGRAPHS = [
    "The memory bank keeps every entry in one PostgreSQL database, and a single "
    "daemon process owns all of its writes. Clients reach it through a small "
    "bridge that speaks the Model Context Protocol, so an agent's tools stay the "
    "same whichever machine the daemon runs on.",
    "A dream pulls the unconsolidated stream, extracts entity, attribute and value "
    "claims, writes them to the cortex and advances a monotonic cursor. Nothing "
    "is silently overwritten: a superseded value stays as audit history, and a "
    "competing value with low trust is parked as a contender instead.",
    "Before a deploy, the update command takes a fresh backup, tags the running "
    "image for rollback, recreates only the daemon container and checks its "
    "health. Official efficiency figures, affluent suffixes and finalised "
    "workflows give the shaper ligatures to form: fi, fl, ffi, ffl.",
]


def pdf(font_dir: Path, *, shaping: bool = False):
    from fpdf import FPDF
    doc = FPDF(format="A4")
    doc.set_auto_page_break(True, margin=18)
    doc.add_font("DejaVu", "", str(font_dir / "DejaVuSans.ttf"))
    doc.add_font("DejaVu", "B", str(font_dir / "DejaVuSans-Bold.ttf"))
    doc.set_font("DejaVu", size=11)
    if shaping:
        doc.set_text_shaping(True)
    return doc


def justified(font_dir: Path):
    doc = pdf(font_dir)
    for page in range(2):
        doc.add_page()
        doc.set_font("DejaVu", "B", 14)
        doc.cell(0, 10, f"Section {page + 1}: operating the bank", new_x="LMARGIN", new_y="NEXT")
        doc.set_font("DejaVu", "", 11)
        for p in PARAGRAPHS:
            doc.multi_cell(0, 6, p, align="J", new_x="LMARGIN", new_y="NEXT")
            doc.ln(2)
    return doc


def shaped(font_dir: Path):
    doc = pdf(font_dir, shaping=True)
    doc.add_page()
    for p in PARAGRAPHS * 2:
        doc.multi_cell(0, 6, p, new_x="LMARGIN", new_y="NEXT")
        doc.ln(2)
    return doc


def columns(font_dir: Path):
    doc = pdf(font_dir)
    doc.add_page()
    with doc.text_columns(ncols=2, gutter=8, text_align="J") as cols:
        for p in PARAGRAPHS * 2:
            cols.write(p + "\n\n")
    return doc


def table(font_dir: Path):
    doc = pdf(font_dir)
    doc.add_page()
    doc.multi_cell(0, 6, "Retention by band, as configured on a default install:",
                   new_x="LMARGIN", new_y="NEXT")
    rows = [("Band", "Capacity", "Retention", "Update interval"),
            ("working", "64", "minutes", "every store"),
            ("short_term", "512", "hours", "every 10 stores"),
            ("long_term", "4096", "weeks", "every dream"),
            ("forever", "unbounded", "permanent", "manual")]
    with doc.table() as t:
        for r in rows:
            row = t.row()
            for cell in r:
                row.cell(cell)
    return doc


def scripts(font_dir: Path):
    doc = pdf(font_dir)
    doc.add_page()
    for line in [
        "English: the quick brown fox jumps over the lazy dog.",
        "Français : « l’été dernier, à la plage » — naïveté et cœur.",
        "Deutsch: Grüße aus München, Ärger über Öl.",
        "Ελληνικά: καλημέρα κόσμε.",
        "Русский: привет, мир.",
        "Symbols: 20 €, 3 × 4 = 12, ≤ ≥ ≠, “quoted” ‘text’.",
    ]:
        doc.multi_cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
    return doc


def long(font_dir: Path):
    doc = pdf(font_dir)
    doc.set_font("DejaVu", size=10)
    for i in range(12):
        doc.add_page()
        doc.set_font("DejaVu", "B", 12)
        doc.cell(0, 8, f"Chapter {i + 1}", new_x="LMARGIN", new_y="NEXT")
        doc.set_font("DejaVu", "", 10)
        for p in PARAGRAPHS * 3:
            doc.multi_cell(0, 5, p, new_x="LMARGIN", new_y="NEXT")
            doc.ln(1)
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--font-dir", type=Path, required=True)
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    for name, make in [("fpdf-justified.pdf", justified), ("fpdf-shaped.pdf", shaped),
                       ("fpdf-columns.pdf", columns), ("fpdf-table.pdf", table),
                       ("fpdf-scripts.pdf", scripts), ("fpdf-long.pdf", long)]:
        doc = make(args.font_dir)
        # A fixed creation date, so a rerun writes the same bytes.
        doc.set_creation_date(datetime(2026, 10, 25, tzinfo=timezone.utc))
        path = OUT / name
        doc.output(str(path))
        print(f"wrote {name} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
