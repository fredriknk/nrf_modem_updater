"""
brady.py – Direct‑print IMEI/IMSI labels to Brady **Direct Print** printers.

Supports two strategies selected with *mode*:

* **bitmap** (default) – render a 600 × 300 px monochrome PNG and send it through
  the OS driver. Works with every BMP‑series printer.
* **bpl** – generate a tiny Brady Printer Language (BPL) script and stream it as
  *RAW* spool data. Preferred for Direct‑Print models (BMP41/51/61/71, i3300,
  i5300, A6500, etc.), avoids driver fuss and prints in < 1 s.
* **auto** – try *bpl* first; if it fails fall back to *bitmap*.

Quick‑start (Windows)::

    import brady

    # list_printers() helps you find the exact queue name
    brady.print_label("352656304560110", "242010123456789", printer_name="Brady BMP41", mode="auto")

Dependencies
------------
• Pillow ≥ 9 (only when mode="bitmap")
• pywin32 ≥ 306 (Windows only)  ``pip install pillow pywin32``
"""

from __future__ import annotations

import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

# Pillow is optional – import lazily in bitmap path
from typing import TYPE_CHECKING

if platform.system() == "Windows":
    try:
        import win32print  # type: ignore
        import win32api  # type: ignore
        import pywintypes  # type: ignore
    except ImportError:  # pragma: no cover – handled at runtime
        win32print = win32api = pywintypes = None  # type: ignore
else:
    win32print = win32api = pywintypes = None  # type: ignore

_LABEL_W = 600
_LABEL_H = 300

# ---------------------------------------------------------------------------
# Bitmap helpers (Pillow)
# ---------------------------------------------------------------------------


def _ensure_pillow():
    global Image, ImageDraw, ImageFont  # noqa: N816 – capitalised by Pillow
    if "Image" in globals():
        return  # already imported
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Pillow is required for bitmap printing – 'pip install pillow'") from exc


def _choose_font(size: int):
    """Pick a monospace font that exists on the host OS."""
    _ensure_pillow()
    for name in (
        "Consolas.ttf",  # Windows
        "DejaVuSansMono.ttf",  # Linux / WSL / macOS brew
        "LiberationMono-Regular.ttf",  # Linux
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_size(draw, text: str, font):
    """Return (w, h) of *text*, Pillow ≥10 safe."""
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)  # Pillow ≥ 8
        return right - left, bottom - top
    except AttributeError:  # pragma: no cover – Pillow < 8 fallback
        return draw.textsize(text, font=font)


def _render_bitmap(imei: str, imsi: str, font_size: int = 60):
    """Compose the label bitmap and return it as a Pillow Image."""
    _ensure_pillow()
    img = Image.new("1", (_LABEL_W, _LABEL_H), color=1)  # 1‑bit white canvas
    draw = ImageDraw.Draw(img)

    font = _choose_font(font_size)
    imei_w, imei_h = _text_size(draw, imei, font)
    imsi_w, imsi_h = _text_size(draw, imsi, font)

    y_imei = (_LABEL_H // 2) - imei_h - 5
    y_imsi = (_LABEL_H // 2) + 5

    draw.text((_LABEL_W // 2 - imei_w // 2, y_imei), imei, font=font, fill=0)
    draw.text((_LABEL_W // 2 - imsi_w // 2, y_imsi), imsi, font=font, fill=0)

    return img


# ---------------------------------------------------------------------------
# BPL helpers
# ---------------------------------------------------------------------------


def _build_bpl_script(imei: str, imsi: str) -> bytes:
    """Return a minimal BPL program as *bytes* (CRLF line endings)."""
    lines = [
        "CLS",  # clear buffer
        f"TXT 40,40,0,5,IMEI:{imei}",
        f"TXT 40,120,0,5,IMSI:{imsi}",
        "PRT",  # print and cut (on benchtop) / eject (on portable)
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


# ---------------------------------------------------------------------------
# Spool helpers (platform‑agnostic)
# ---------------------------------------------------------------------------


def _send_png(image_path: Path, printer_name: Optional[str] = None):
    """Send a PNG file through the OS print pipeline."""

    system = platform.system()

    if system == "Windows":
        if win32api is None:
            raise RuntimeError("pywin32 is required on Windows to print")

        verb = "printto" if printer_name else "print"
        params = f'"{printer_name}"' if printer_name else None
        try:
            win32api.ShellExecute(0, verb, str(image_path), params, ".", 0)
        except (pywintypes.error, RuntimeError) as exc:
            # 31 = «A device attached to the system is not functioning.» → driver lacks verb
            if isinstance(exc, pywintypes.error) and exc.winerror != 31:
                raise
            # Fallback: MS Paint CLI (works universally)
            queue = printer_name or (win32print.GetDefaultPrinter() if win32print else None)
            if queue is None:
                raise RuntimeError("No default printer configured; specify printer_name")
            subprocess.run(["mspaint.exe", "/pt", str(image_path), queue], check=True)

    else:  # macOS / Linux – use CUPS lpr
        cmd = ["lpr", "-o", "raw"]
        if printer_name:
            cmd += ["-P", printer_name]
        cmd.append(str(image_path))
        subprocess.run(cmd, check=True)


def _send_raw(data: bytes, printer_name: Optional[str] = None):
    """Send *data* directly to the printer queue as RAW bytes."""

    system = platform.system()

    if system == "Windows":
        if win32print is None:
            raise RuntimeError("pywin32 is required on Windows to print RAW data")

        queue = printer_name or win32print.GetDefaultPrinter()
        if not queue:
            raise RuntimeError("No default printer configured; specify printer_name")

        hPrinter = win32print.OpenPrinter(queue)
        try:
            hJob = win32print.StartDocPrinter(hPrinter, 1, ("BPL Label", None, "RAW"))
            win32print.StartPagePrinter(hPrinter)
            win32print.WritePrinter(hPrinter, data)
            win32print.EndPagePrinter(hPrinter)
            win32print.EndDocPrinter(hPrinter)
        finally:
            win32print.ClosePrinter(hPrinter)
    else:  # POSIX CUPS
        cmd = ["lpr", "-o", "raw"]
        if printer_name:
            cmd += ["-P", printer_name]
        subprocess.run(cmd, input=data, check=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_printers() -> List[str]:
    """Return the list of printer queues visible to the OS (Windows only)."""
    print("Available printers:")
    if platform.system() == "Windows" and win32print is not None:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return [p[2] for p in win32print.EnumPrinters(flags)]
    return []


def print_label(
    imei: str,
    imsi: str,
    *,
    printer_name: Optional[str] = None,
    mode: str = "bitmap",  # "bitmap" | "bpl" | "auto"
):
    """Print a two‑line IMEI/IMSI label using the selected *mode*.

    *mode*:
        • "bitmap" – always send PNG (universal, slower on Direct‑Print models)
        • "bpl"     – always send BPL RAW (fails if the printer doesn’t understand BPL)
        • "auto"    – try BPL first, fall back to bitmap on error
    """

    imei = imei.strip()
    imsi = imsi.strip()

    if not (imei.isdigit() and len(imei) in (14, 15)):
        raise ValueError("IMEI must be a 14/15‑digit decimal string")
    if not (imsi.isdigit() and len(imsi) in (15, 16)):
        raise ValueError("IMSI must be a 15/16‑digit decimal string")

    def _bitmap_path():
        _ensure_pillow()
        img = _render_bitmap(imei, imsi)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        img.save(tmp.name, "PNG")
        return Path(tmp.name)

    def _do_bitmap():
        path = _bitmap_path()
        try:
            _send_png(path, printer_name)
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    if mode == "bitmap":
        _do_bitmap()

    elif mode == "bpl":
        script = _build_bpl_script(imei, imsi)
        _send_raw(script, printer_name)

    elif mode == "auto":
        try:
            script = _build_bpl_script(imei, imsi)
            _send_raw(script, printer_name)
        except Exception:
            _do_bitmap()
    else:
        raise ValueError("mode must be 'bitmap', 'bpl', or 'auto'")


#__all__ = ["print_label", "list_printers"]

if __name__ == "__main__":
    list_printers()
    #print_label("352656304560110", "242010123456789",printer_name="Brady BMP41", mode="bitmap")
    #print_label("352656304560110", "242010123456789", printer_name="BMP41", mode="bpl")          # or mode="auto" to let the helper choose