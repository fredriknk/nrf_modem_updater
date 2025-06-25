# brady.py – super‑minimal BMP41 test print helper (Windows‑only)
# -------------------------------------------------------------
# Just render a tiny bitmap with Pillow, then fire it through
# Windows Paint’s `/pt` switch so the Brady BMP‑series spits it out.
# No Linux, no BPL, no extras.

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont):
    """Return (w, h) using the Pillow‑10‑safe API."""
    try:
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        return r - l, b - t
    except AttributeError:  # Pillow < 10 fallback
        return draw.textsize(text, font=font)


def print_label(
    imei: str,
    imsi: str,
    *,
    printer_name: str = "Brady BMP41",  # queue name as shown in Windows
    label_width: int = 30,
    label_height: int =10,
    font_size: int = 6,
):
    """Render IMEI/IMSI and send to *printer_name* via mspaint /pt."""

    # Basic sanity – keep it decimal, strip spaces
    imei = ''.join(filter(str.isdigit, imei))
    imsi = ''.join(filter(str.isdigit, imsi))

    img = Image.new("1", (label_width, label_height), 1)  # 1‑bit white
    draw = ImageDraw.Draw(img)

    # Font: try Consolas (Windows), else Pillow default
    try:
        font = ImageFont.truetype("Consolas.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    w1, h1 = _text_size(draw, imei, font)
    w2, h2 = _text_size(draw, imsi, font)

    y1 = (label_height // 2) - h1  # upper half
    y2 = (label_height // 2) + 5   # lower half

    draw.text(((label_width - w1) // 2, y1), imei, font=font, fill=0)
    draw.text(((label_width - w2) // 2, y2), imsi, font=font, fill=0)

    # Spool via Paint (works even when driver lacks a "Print" verb)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        img.save(tmp.name, "PNG")
    subprocess.run(["mspaint.exe", "/pt", tmp.name, printer_name], check=True)
    Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    # Dead‑simple smoke test – adjust printer_name if needed
    print_label("490154203237518", "310260123456789")
