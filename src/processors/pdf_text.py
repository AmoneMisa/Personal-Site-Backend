from __future__ import annotations

import re
from typing import Any, Dict, List

import fitz  # PyMuPDF (pulled in transitively by pdf2docx)

# PyMuPDF span "flags" bitmask (see get_text("dict") docs)
_FLAG_ITALIC = 1 << 1  # 2
_FLAG_BOLD = 1 << 4  # 16

# Auto-link detection: bare URLs and e-mail addresses embedded as plain text.
_URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
# trailing punctuation that shouldn't be part of a detected link
_TRAILING = ".,;:!?)]}'\""


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


def _matches_in_span(text: str) -> List[Dict[str, Any]]:
    """Return [{start, end, uri}] for every URL / e-mail found in a span's text."""
    found: List[Dict[str, Any]] = []

    for m in _URL_RE.finditer(text):
        token = m.group(1).rstrip(_TRAILING)
        if token:
            found.append({"start": m.start(1), "end": m.start(1) + len(token), "uri": token})

    for m in _EMAIL_RE.finditer(text):
        token = m.group(1).rstrip(_TRAILING)
        if token:
            found.append({"start": m.start(1), "end": m.start(1) + len(token), "uri": f"mailto:{token}"})

    return found


def extract_links(src_pdf: str, page: int, dpi: int = 144) -> List[Dict[str, Any]]:
    """
    Collect clickable link regions for a 1-based page, in rendered-PNG pixel
    space at `dpi` (top-left origin), so they align with the preview/overlay.

    Sources:
      * real URI link annotations already present in the source PDF
      * bare URLs / e-mail addresses that appear as plain text (auto-detected)

    Each item: {x, y, w, h, uri}. Duplicate URIs on overlapping rects are dropped.
    """
    scale = (dpi if dpi > 0 else 72) / 72.0

    doc = fitz.open(src_pdf)
    try:
        idx = page - 1
        if idx < 0 or idx >= doc.page_count:
            return []

        pg = doc.load_page(idx)
        out: List[Dict[str, Any]] = []

        # 1) real link annotations
        for lnk in pg.get_links():
            uri = lnk.get("uri")
            rect = lnk.get("from")
            if not uri or rect is None:
                continue
            out.append(
                {
                    "x": round(rect.x0 * scale, 2),
                    "y": round(rect.y0 * scale, 2),
                    "w": round((rect.x1 - rect.x0) * scale, 2),
                    "h": round((rect.y1 - rect.y0) * scale, 2),
                    "uri": uri,
                }
            )

        # 2) auto-detected links inside plain-text spans
        data = pg.get_text("dict")
        for blk in data.get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text") or ""
                    if not text.strip():
                        continue
                    matches = _matches_in_span(text)
                    if not matches:
                        continue

                    x0, y0, x1, y1 = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    span_w = max(1e-3, x1 - x0)
                    n = max(1, len(text))
                    # character width is approximated uniformly across the span
                    cw = span_w / n

                    for mt in matches:
                        mx0 = x0 + cw * mt["start"]
                        mx1 = x0 + cw * mt["end"]
                        out.append(
                            {
                                "x": round(mx0 * scale, 2),
                                "y": round(y0 * scale, 2),
                                "w": round((mx1 - mx0) * scale, 2),
                                "h": round((y1 - y0) * scale, 2),
                                "uri": mt["uri"],
                            }
                        )

        # dedupe: same uri + near-identical rect
        deduped: List[Dict[str, Any]] = []
        for item in out:
            dup = False
            for kept in deduped:
                if kept["uri"] == item["uri"] and abs(kept["x"] - item["x"]) < 2 and abs(kept["y"] - item["y"]) < 2:
                    dup = True
                    break
            if not dup:
                deduped.append(item)

        return deduped
    finally:
        doc.close()
