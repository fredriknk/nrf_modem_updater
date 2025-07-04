# brady.py – tiny BMP41 helper with **preview** and sane size defaults (Windows‑only)
# ---------------------------------------------------------------------------
# Generates a 384 × 128 px 1‑bit bitmap (≈ 25 mm × 8 mm at 300 dpi) and either
# shows it for preview or prints it via *mspaint /pt* to the Brady BMP41.
#
# Dependencies: Pillow (pip install pillow)

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# Reasonable defaults for a BMP41 19 mm tape @ 300 dpi
inch = 25.4
dpi = 30
width  = 30 # mm
height = 12.7 # mm
_FONTSIZE = 8  # pt, default for BMP41
_LABEL_W = int((width/inch)*dpi)   # pixels (~32 mm across the tape width)
_LABEL_H = int((height/inch)*dpi)   # pixels (~11 mm of feed length)
_DEF_FONT = "Consolas.ttf"


def _load_font(max_width: int, max_height: int, text: str, font_path: str = _DEF_FONT):
    """Find the largest font size that lets *text* fit within *max_width*."""
    # Try big → small until it fits
    for size in range(72, 4, -2):
        try:
            font = ImageFont.truetype(font_path, size)
        except OSError:
            font = ImageFont.load_default()
            break
        w, h = font.getbbox(text)[2:]
        if w <= max_width and h <= max_height:
            return font
    return ImageFont.load_default()


def _render_label(imei: str, imsi: str, w: int, h: int) -> Image.Image:
    """Return a PIL Image with the two numbers centered one above the other."""
    img = Image.new("1", (w, h), 1)  # white background, 1‑bit
    draw = ImageDraw.Draw(img)

    # Pick two independent font sizes that fill half the height each
    font1 = _load_font(w - 10, h // 2 , imei)
    font2 = _load_font(w - 10, h // 2 , imsi)

    w1, h1 = draw.textbbox((0, 0), imei, font=font1)[2:]
    w2, h2 = draw.textbbox((0, 0), imsi, font=font2)[2:]

    y1 = (h // 4) - (h1 // 2)
    y2 = (3 * h // 4) - (h2 // 2)

    draw.text(((w - w1) // 2, y1), imei, font=font1, fill=0)
    draw.text(((w - w2) // 2, y2), imsi, font=font2, fill=0)
    return img


def print_label(
    imei: str,
    imsi: str,
    *,
    printer_name: str = "Brady BMP41",
    label_width: int = _LABEL_W,
    label_height: int = _LABEL_H,
    preview: bool = False,
):
    """Preview or print a simple IMEI/IMSI label on a BMP41.

    Set *preview=True* to just open the image without printing.
    """
    imei = "".join(filter(str.isdigit, imei))
    imsi = "".join(filter(str.isdigit, imsi))

    img = _render_label(imei, imsi, label_width, label_height)

    if preview:
        img.show()  # opens with the default image viewer
        return

    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        img.save(tmp.name, "PNG")
    try:
        subprocess.run(["mspaint.exe", tmp.name, printer_name], check=True)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    # Smoke test
    print_label("490154203237518", "310260123456789", preview=True)
    #print_label("490154203237518", "310260123456789")