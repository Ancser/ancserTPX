"""Generate the full-UI liquid glass demo from the real ancserTPX.html.

The demo is a byte-for-byte copy of the production page plus four
injections, so it cannot drift in content — re-run this after editing
ancserTPX.html:

    python scripts/build_glass_demo.py

Why a generated copy rather than a hand-maintained fork: the point of
the demo is to see the REAL UI under glass. A fork of an 87KB page
would be stale within a day, and any difference would be mistaken for
a glass problem.

Script order is load-bearing. ancserTPX.js binds tab behaviour inside a
DOMContentLoaded handler (`t.onclick = ...`), so it must run BEFORE the
skin re-parents those nodes — the skin moves elements precisely so
those bindings survive. Then tpx-glass.js mounts surfaces onto whatever
the skin produced. Hence: app -> skin -> glass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend" / "static" / "ancserTPX.html"
TARGET = ROOT / "frontend" / "static" / "demos" / "tpx-full-glass-demo.html"

VERSION = "glassdemo7"

BANNER = """<!--
  ============================================================
  GENERATED FILE — do not edit by hand.
  Source: frontend/static/ancserTPX.html
  Regenerate: python scripts/build_glass_demo.py

  Full ancserTPX UI with the liquid glass skin applied on top.
  Everything here except the four injected tags is the real page.
  ============================================================
-->
"""

FILTER_ROOT = (
    '\n<!-- Liquid glass engine filter host (generated feDisplacementMap chains). -->\n'
    '<svg class="filter-root" aria-hidden="true" focusable="false">'
    '<defs id="filterDefs"></defs></svg>\n'
)

SCRIPTS = (
    '\n<!-- Skin first: it re-parents the nodes ancserTPX.js has already bound. -->\n'
    f'<script src="/static/demos/tpx-glass-skin.js?v={VERSION}"></script>\n'
    f'<script src="/static/demos/tpx-glass.js?v={VERSION}"></script>\n'
)


def fail(message: str) -> None:
    sys.exit(f"build_glass_demo: {message}")


def main() -> None:
    if not SOURCE.exists():
        fail(f"missing source {SOURCE}")
    html = SOURCE.read_text(encoding="utf-8")

    # 1. title
    html, n = re.subn(
        r"<title>.*?</title>",
        "<title>ancserTPX · full UI liquid glass demo</title>",
        html,
        count=1,
        flags=re.S,
    )
    if not n:
        fail("no <title> found")

    # 2. glass stylesheet, immediately after the production one so the
    #    glass layer can override it without !important everywhere.
    html, n = re.subn(
        r'(<link[^>]*href="/static/ancserTPX\.css[^"]*"[^>]*>)',
        r'\1\n    <link rel="stylesheet" href="/static/demos/tpx-glass.css?v='
        + VERSION
        + '">',
        html,
        count=1,
    )
    if not n:
        fail("no ancserTPX.css <link> found")

    # 3. filter host right after <body>
    html, n = re.subn(r"(<body[^>]*>)", r"\1" + FILTER_ROOT, html, count=1)
    if not n:
        fail("no <body> found")

    # 4. skin + engine after the app script (which must bind first)
    html, n = re.subn(
        r'(<script src="/static/ancserTPX\.js[^"]*"></script>)',
        r"\1" + SCRIPTS,
        html,
        count=1,
    )
    if not n:
        fail("no ancserTPX.js <script> found")

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(BANNER + html, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}  ({TARGET.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
