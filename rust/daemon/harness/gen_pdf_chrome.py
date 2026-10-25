"""Write the browser-printed PDFs of the PDF corpus (``pdf_corpus/chrome-*.pdf``).

Real-producer output for the PDF differential, from text written here (so
the files can be committed): Chrome's print-to-PDF (Skia), with subset
TrueType/CID fonts, kerning and ligatures, CSS hyphenation (line-end
hyphens, which PDFium's text API reports as U+FFFE), multi-column layout,
a table, several scripts (Greek, Cyrillic, CJK, Arabic, Hebrew) and a
character outside the BMP. Run on Windows with Chrome installed; the
output embeds a creation date, so a rerun rewrites the files, and the
goldens (``gen_pdf_goldens.py``) are regenerated with them.

    python rust/daemon/harness/gen_pdf_chrome.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
OUT = Path(__file__).with_name("pdf_corpus")

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
<style>body{{font-family:Georgia,serif;font-size:13pt;width:9cm;text-align:justify;
hyphens:auto}}</style>
<p lang="en">{PROSE * 4}</p>
<p lang="en">Incomprehensibilities, internationalization and
counterrevolutionaries break across lines when the column is narrow.</p>
""",
    "chrome-table": """
<style>body{font-family:Arial,sans-serif}td,th{border:1px solid #444;padding:4px}</style>
<h1>Quarterly figures</h1>
<table><tr><th>Quarter</th><th>Entries</th><th>Facts</th><th>Share</th></tr>
<tr><td>Q1</td><td>1,204</td><td>311</td><td>25.8%</td></tr>
<tr><td>Q2</td><td>2,018</td><td>476</td><td>23.6%</td></tr>
<tr><td>Q3</td><td>3,551</td><td>902</td><td>25.4%</td></tr></table>
<p>Totals: 6,773 entries and 1,689 facts.</p>
""",
    "chrome-columns": f"""
<style>body{{font-family:Verdana,sans-serif;column-count:3;column-gap:1cm;font-size:10pt}}</style>
<h2>Three columns</h2><p>{PROSE * 6}</p>
""",
    "chrome-scripts": """
<style>body{font-family:'Segoe UI','Yu Gothic','Microsoft YaHei',sans-serif;font-size:14pt}</style>
<p lang="el">Ελληνικά: η μνήμη κρατά κάθε γεγονός με την ιστορία του.</p>
<p lang="ru">Русский: память хранит каждый факт вместе с его историей.</p>
<p lang="ja">日本語：記憶はすべての事実をその履歴とともに保持します。</p>
<p lang="zh">中文：记忆保存每一个事实及其历史。</p>
<p lang="ar" dir="rtl">العربية: الذاكرة تحفظ كل حقيقة مع تاريخها.</p>
<p lang="he" dir="rtl">עברית: הזיכרון שומר כל עובדה עם ההיסטוריה שלה.</p>
<p>Outside the BMP: 𝔐𝔢𝔪𝔬𝔯𝔶 and 🧠 (U+1D510 onward, U+1F9E0).</p>
""",
    "chrome-long": "<style>body{font-family:Calibri,sans-serif;font-size:12pt}</style>"
    + "".join(f"<h2>Section {i}</h2><p>{PROSE * 3}</p>" for i in range(1, 25)),
}


def main() -> int:
    if not CHROME.exists():
        sys.exit(f"needs Chrome at {CHROME}")
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in PAGES.items():
            html = Path(tmp, f"{name}.html")
            html.write_text(f"<!doctype html><html><head><meta charset='utf-8'>"
                            f"<title>{name}</title></head><body>{body}</body></html>",
                            encoding="utf-8")
            out = OUT / f"{name}.pdf"
            subprocess.run([str(CHROME), "--headless=new", "--disable-gpu",
                            "--no-pdf-header-footer", f"--user-data-dir={Path(tmp, 'profile')}",
                            f"--print-to-pdf={out}", html.as_uri()],
                           check=True, capture_output=True, timeout=120)
            print(f"{out.name}: {out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
