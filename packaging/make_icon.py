"""Generate ``packaging/dropwatch.ico`` — the app icon for the exe and window.

Run after changing :func:`dropwatch.desktop.icon_image`::

    .venv/Scripts/python packaging/make_icon.py

The drawing lives in ``desktop.py`` rather than here so the tray, the window and
the executable cannot drift apart: the tray renders it at runtime (no asset to go
missing from a bundle), and this bakes the identical drawing into a .ico for the
two places Windows insists on a file.

Checked in rather than generated at build time — building the exe should not
require Pillow to be importable by PyInstaller's own interpreter, and an icon
that changes only when someone edits the drawing belongs in review.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dropwatch.desktop import icon_image  # noqa: E402

#: Every size Windows picks from: 16 in the tray and title bar, 32 in the taskbar
#: and alt-tab, 48 in Explorer's medium view, 256 for the large views and the
#: installer. Shipping fewer means Windows downscales a big one and the result
#: turns to mush exactly where it is smallest.
SIZES = (16, 24, 32, 48, 64, 128, 256)

OUT = ROOT / "packaging" / "dropwatch.ico"
PREVIEW = ROOT / "packaging" / "icon-preview.png"


def main() -> int:
    from PIL import Image

    # Render each size from scratch instead of downscaling one master: the mark
    # is drawn proportionally, so a 16px render keeps its proportions where a
    # 16px downscale of a 256px render would smear them.
    images = [icon_image(size) for size in SIZES]

    largest = images[-1]
    largest.save(OUT, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    print(f"wrote {OUT.relative_to(ROOT)}  ({', '.join(str(s) for s in SIZES)}px)")

    # A side-by-side sheet, so the small sizes can actually be judged.
    pad, scale = 12, 2
    width = sum(s * scale + pad for s in SIZES) + pad
    sheet = Image.new("RGBA", (width, 256 * scale + pad * 2), (24, 28, 38, 255))
    x = pad
    for size, image in zip(SIZES, images, strict=True):
        big = image.resize((size * scale, size * scale), Image.NEAREST)
        sheet.alpha_composite(big, (x, pad))
        x += size * scale + pad
    sheet.save(PREVIEW)
    print(f"wrote {PREVIEW.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
