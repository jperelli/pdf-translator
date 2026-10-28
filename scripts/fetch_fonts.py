"""Download the bundled similar-font candidates (all SIL Open Font License) into app/fonts/.

    python scripts/fetch_fonts.py
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

from app.formats.pdf.fontmatch import BUNDLED, css_url

OUT = Path(__file__).resolve().parents[1] / "app" / "fonts"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for family, weights in BUNDLED.items():
        css = urllib.request.urlopen(css_url(family, weights), timeout=30).read().decode()
        for weight, url in re.findall(r"font-weight: (\d+);.*?src: url\((\S+?\.ttf)\)", css, re.S):
            dest = OUT / f"{family.replace(' ', '')}-{weight}.ttf"
            dest.write_bytes(urllib.request.urlopen(url, timeout=60).read())
            print(dest.name, dest.stat().st_size)


if __name__ == "__main__":
    main()
