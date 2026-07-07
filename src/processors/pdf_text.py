from __future__ import annotations

from typing import Any, Dict, List

import fitz  # PyMuPDF (pulled in transitively by pdf2docx)

# PyMuPDF span "flags" bitmask (see get_text("dict") docs)
_FLAG_ITALIC = 1 << 1  # 2
_FLAG_BOLD = 1 << 4  # 16


def _color_to_hex(color: int | None) -> str:
    """PyMuPDF encodes span colour as a packed sRGB integer (0xRRGGBB)."""
    if not isinstance(color, int):
        return "#111111"
    r = (color >> 16) & 255
    g = (color >> 8) & 255
    b = color & 255
    return f"#{r:02x}{g:02x}{b:02x}"


def extract_text_blocks(src_pdf: str, page: int, dpi: int = 144) -> List[Dict[str, Any]]:
    """
    Extract per-span editable text blocks for a single 1-based page.

    Coordinates are returned in the rendered-PNG pixel space at `dpi`
    (top-left origin), matching the Ghostscript preview the frontend overlays,
    so the client can place each block directly onto the canvas.
    """
    scale = (dpi if dpi > 0 else 72) / 72.0

    doc = fitz.open(src_pdf)
    try:
        idx = page - 1
        if idx < 0 or idx >= doc.page_count:
            return []

        pg = doc.load_page(idx)
        data = pg.get_text("dict")

        blocks: List[Dict[str, Any]] = []
        seq = 0

        for blk in data.get("blocks", []):
            # type 0 == text block; images/other are skipped
            if blk.get("type", 0) != 0:
                continue

            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    text = (span.get("text") or "").strip()
                    if not text:
                        continue

                    x0, y0, x1, y1 = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    flags = int(span.get("flags", 0))

                    blocks.append(
                        {
                            "id": f"blk_{page}_{seq}",
                            "x": round(x0 * scale, 2),
                            "y": round(y0 * scale, 2),
                            "w": round((x1 - x0) * scale, 2),
                            "h": round((y1 - y0) * scale, 2),
                            "text": text,
                            "fontSize": round(float(span.get("size", 12.0)) * scale, 2),
                            "fontName": str(span.get("font", "Helvetica")),
                            "bold": bool(flags & _FLAG_BOLD),
                            "italic": bool(flags & _FLAG_ITALIC),
                            "color": _color_to_hex(span.get("color")),
                        }
                    )
                    seq += 1

        return blocks
    finally:
        doc.close()
