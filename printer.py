# brady.py – BMP41 quick‑print helper with **interactive Tk preview** (Windows‑only)
# ---------------------------------------------------------------------------
# • Generates a tiny 1‑bit label bitmap (default 384 × 128 px ≈ 32 × 11 mm).
# • If *preview=True* it pops up a Tk window showing the label at 3× scale
#   with a **Print** button that immediately spools to the BMP41.
# • Otherwise (*preview=False*, default) it prints head‑less via *mspaint /pt*.
#
# Install: `pip install pillow`

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageTk

try:
    import tkinter as tk
except ImportError:  # head‑less interpreter (CI), disable preview functionality
    tk = None  # type: ignore

# --- defaults ---------------------------------------------------------------
_LABEL_W = 384   # pixels  (≈32 mm across 19 mm tape @ 300 dpi)
_LABEL_H = 128   # pixels  (≈11 mm feed length)
_FONT_PATH = "Consolas.ttf"  # falls back to PIL’s default if missing
_SCALE = 3       # GUI preview magnification factor


# --- helpers ----------------------------------------------------------------

def _best_font(max_w: int, max_h: int, text: str) -> ImageFont.FreeTypeFont:
    """Return the largest font that keeps *text* within *max_w*×*max_h*."""
    for size in range(72, 4, -2):
        try:
            f = ImageFont.truetype(_FONT_PATH, size)
        except OSError:
            f = ImageFont.load_default()
            return f
        w, h = f.getbbox(text)[2:]
        if w <= max_w and h <= max_h:
            return f
    return ImageFont.load_default()


def _render(imei: str, imsi: str, w: int, h: int) -> Image.Image:
    img = Image.new("1", (w, h), 1)  # 1‑bit white
    d = ImageDraw.Draw(img)

    f1 = _best_font(w - 10, h // 2 - 5, imei)
    f2 = _best_font(w - 10, h // 2 - 5, imsi)

    w1, h1 = d.textbbox((0, 0), imei, font=f1)[2:]
    w2, h2 = d.textbbox((0, 0), imsi, font=f2)[2:]

    d.text(((w - w1) // 2, (h // 4) - (h1 // 2)), imei, font=f1, fill=0)
    d.text(((w - w2) // 2, (3 * h // 4) - (h2 // 2)), imsi, font=f2, fill=0)
    return img


def _mspaint_print(path: Path, printer: str):
    subprocess.run(["mspaint.exe", "/pt", str(path), printer], check=True)


# --- public -----------------------------------------------------------------

def print_label(
    imei: str,
    imsi: str,
    *,
    printer_name: str = "Brady BMP41",
    label_width: int = _LABEL_W,
    label_height: int = _LABEL_H,
    preview: bool = False,
):
    """Render an IMEI / IMSI label and either *print* or *preview* it.

    Parameters
    ----------
    preview : bool
        • **True**  → open a Tk window (3× zoom) with a **Print** button.
        • **False** → send directly to *printer_name* via *mspaint /pt*.
    """

    imei = "".join(filter(str.isdigit, imei))
    imsi = "".join(filter(str.isdigit, imsi))

    img = _render(imei, imsi, label_width, label_height)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        img.save(tmp.name, "PNG", dpi=(300, 300))  # embed DPI for host apps
    png_path = Path(tmp.name)

    try:
        if preview and tk:
            _show_preview(img, png_path, printer_name)
        else:
            _mspaint_print(png_path, printer_name)
    finally:
        png_path.unlink(missing_ok=True)


# --- GUI preview ------------------------------------------------------------

def _show_preview(img: Image.Image, png_path: Path, printer_name: str):
    """Interactive 3× zoom preview with a *Print* button."""
    root = tk.Tk()
    root.title("Label preview – {}".format(printer_name))

    big = img.resize((img.width * _SCALE, img.height * _SCALE), Image.NEAREST)
    tk_img = ImageTk.PhotoImage(big.convert("RGB"))

    tk.Label(root, image=tk_img).pack(padx=10, pady=10)

    def _do_print():
        root.destroy()
        _mspaint_print(png_path, printer_name)

    tk.Button(root, text="Print", command=_do_print, width=12).pack(pady=(0, 10))
    root.mainloop()


# --- CLI smoke test ---------------------------------------------------------
if __name__ == "__main__":
    print_label("490154203237518", "310260123456789", preview=True)
