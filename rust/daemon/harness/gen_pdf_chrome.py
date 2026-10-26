"""Write the browser-printed PDFs of the PDF corpus (``pdf_corpus/chrome-*.pdf``).

Real-producer output for the PDF differential, from text written here and
set only in the repository's own Geist fonts (SIL OFL 1.1,
``frontend/src/fonts/``), so the files can be committed: Chrome's
print-to-PDF (Skia) with subset CID fonts, kerning and ligatures, CSS
hyphenation (line-end hyphens, which PDFium's text API reports as
U+FFFE), multi-column layout, a table, and the scripts Geist covers (Latin
with accents, Cyrillic); the hand-made corpus covers Greek and Hebrew in
DejaVu. The generator refuses a file that embeds any other font. Run with
Chrome installed; the output embeds a creation date, so a rerun rewrites
the files, and the goldens (``gen_pdf_goldens.py``) are regenerated with
them.

    python rust/daemon/harness/gen_pdf_chrome.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
OUT = Path(__file__).with_name("pdf_corpus")
FONTS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "fonts"
# Geist for prose, Geist Mono for the table; nothing else may be embedded.
FACES = ("@font-face{font-family:GeistLocal;src:url(Geist-Variable.woff2)}"
         "@font-face{font-family:GeistMonoLocal;src:url(GeistMono-Variable.woff2)}")

PROSE = (
    "Memory is not a filing cabinet. The daemon keeps every entry with the "
    "episode that wrote it, ranks what it finds by meaning rather than by "
    "spelling, and lets a newer fact supersede an older one without "
    "deleting the history. Office workflows, efficient affinity, fluffy "
    "waffles and difficult official offices exercise ligatures; AVATAR, "
    "WAVE, Toyota and LTA exercise kerning pairs. "
)

PAGES = {
    "chrome-prose": f"""
<style>body{{font-family:GeistLocal;font-size:13pt;width:9cm;text-align:justify;
hyphens:auto}}</style>
<p lang="en">{PROSE * 4}</p>
<p lang="en">Incomprehensibilities, internationalization and
counterrevolutionaries break across lines when the column is narrow.</p>
""",
    "chrome-table": """
<style>body{font-family:GeistMonoLocal}td,th{border:1px solid #444;padding:4px}</style>
<h1>Quarterly figures</h1>
<table><tr><th>Quarter</th><th>Entries</th><th>Facts</th><th>Share</th></tr>
<tr><td>Q1</td><td>1,204</td><td>311</td><td>25.8%</td></tr>
<tr><td>Q2</td><td>2,018</td><td>476</td><td>23.6%</td></tr>
<tr><td>Q3</td><td>3,551</td><td>902</td><td>25.4%</td></tr></table>
<p>Totals: 6,773 entries and 1,689 facts.</p>
""",
    "chrome-columns": f"""
<style>body{{font-family:GeistLocal;column-count:3;column-gap:1cm;font-size:10pt}}</style>
<h2>Three columns</h2><p>{PROSE * 6}</p>
""",
    "chrome-scripts": """
<style>body{font-family:GeistLocal;font-size:14pt}</style>
<p lang="ru">Русский: память хранит каждый факт вместе с его историей.</p>
<p lang="uk">Українська: пам’ять зберігає кожен факт разом з його історією.</p>
<p lang="pl">Polski: pamięć przechowuje każdy fakt wraz z jego historią.</p>
<p lang="hu">Magyar: az emlékezet minden tényt őriz a történetével együtt.</p>
<p lang="fr">Français : « la mémoire garde chaque fait » — œuvre, cœur, naïve.</p>
""",
    "chrome-long": "<style>body{font-family:GeistLocal;font-size:12pt}</style>"
    + "".join(f"<h2>Section {i}</h2><p>{PROSE * 3}</p>" for i in range(1, 25)),
}


def embedded_fonts(pdf: bytes) -> set[str]:
    """Every /BaseFont and /FontName in the file, compressed streams
    included."""
    chunks = [pdf]
    for m in re.finditer(rb"stream\r?\n", pdf):
        end = pdf.find(b"endstream", m.end())
        try:
            chunks.append(zlib.decompress(pdf[m.end():end]))
        except zlib.error:
            pass
    names = set()
    for chunk in chunks:
        for m in re.finditer(rb"/(?:BaseFont|FontName)\s*/([^\s/<>\[\]()]+)", chunk):
            names.add(m.group(1).decode("latin-1").split("+", 1)[-1])
    return names


def main() -> int:
    if not CHROME.exists():
        sys.exit(f"needs Chrome at {CHROME}")
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in PAGES.items():
            html = Path(tmp, f"{name}.html")
            for font in ("Geist-Variable.woff2", "GeistMono-Variable.woff2"):
                shutil.copyfile(FONTS / font, Path(tmp, font))
            html.write_text(f"<!doctype html><html><head><meta charset='utf-8'>"
                            f"<title>{name}</title><style>{FACES}</style></head>"
                            f"<body>{body}</body></html>", encoding="utf-8")
            out = OUT / f"{name}.pdf"
            subprocess.run([str(CHROME), "--headless=new", "--disable-gpu",
                            "--no-pdf-header-footer", "--allow-file-access-from-files",
                            f"--user-data-dir={Path(tmp, 'profile')}",
                            f"--print-to-pdf={out}", html.as_uri()],
                           check=True, capture_output=True, timeout=120)
            fonts = embedded_fonts(out.read_bytes())
            if not fonts or any("Geist" not in f for f in fonts):
                out.unlink()
                sys.exit(f"{out.name} embeds {sorted(fonts)}; only Geist may be embedded")
            print(f"{out.name}: {out.stat().st_size} bytes, fonts {sorted(fonts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
