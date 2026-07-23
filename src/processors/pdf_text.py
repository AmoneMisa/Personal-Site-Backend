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


def _dominant(counter: Dict[Any, float]) -> Any:
    """Return the key with the greatest accumulated weight (char count)."""
    if not counter:
        return None
    return max(counter.items(), key=lambda kv: kv[1])[0]


def _merge_counter(dst: Dict[Any, float], src: Dict[Any, float]) -> None:
    """Accumulate `src`'s weights into `dst` (used when joining paragraphs)."""
    for k, v in src.items():
        dst[k] = dst.get(k, 0.0) + v


# Paragraph-merge tolerances, all in unscaled PDF points.
_COL_TOL = 3.0        # max left-edge drift for two lines to share a column
_SIZE_TOL = 1.5       # max font-size difference to treat lines as one paragraph
_GAP_FACTOR = 0.8     # max blank vertical gap as a fraction of the line's size
_OVERLAP_FACTOR = 0.4  # allow slight bbox overlap between stacked lines


def extract_text_blocks(src_pdf: str, page: int, dpi: int = 144) -> List[Dict[str, Any]]:
    """
    Extract editable text objects for a single 1-based page, one per PyMuPDF
    text block (i.e. one per paragraph), NOT one per span/line/word.

    Each block joins its lines with newlines and unions their bounding boxes, so
    the frontend gets a compact, paragraph-sized Textbox instead of dozens of
    separate row/word fragments. Typography (font size, family, bold, italic,
    colour) is the character-count-weighted dominant value across the block's
    spans, so the most-used style wins for mixed-run paragraphs.

    Coordinates are returned in the rendered-PNG pixel space at `dpi`
    (top-left origin), matching the Ghostscript preview the frontend overlays.
    """
    scale = (dpi if dpi > 0 else 72) / 72.0

    doc = fitz.open(src_pdf)
    try:
        idx = page - 1
        if idx < 0 or idx >= doc.page_count:
            return []

        pg = doc.load_page(idx)
        data = pg.get_text("dict")

        # Phase 1: one record per PyMuPDF text block. Design/CV PDFs frequently
        # emit a separate block for every visual line, so a block here is just a
        # paragraph *candidate* that Phase 2 may still merge with its neighbours.
        raw: List[Dict[str, Any]] = []

        for blk in data.get("blocks", []):
            # type 0 == text block; images/other are skipped
            if blk.get("type", 0) != 0:
                continue

            line_texts: List[str] = []
            bx0 = by0 = float("inf")
            bx1 = by1 = float("-inf")

            # char-count-weighted tallies for the dominant style of the block
            size_w: Dict[float, float] = {}
            font_w: Dict[str, float] = {}
            bold_w: Dict[bool, float] = {}
            italic_w: Dict[bool, float] = {}
            color_w: Dict[str, float] = {}

            for line in blk.get("lines", []):
                span_texts: List[str] = []
                for span in line.get("spans", []):
                    text = span.get("text") or ""
                    if not text.strip():
                        continue

                    x0, y0, x1, y1 = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    bx0 = min(bx0, x0)
                    by0 = min(by0, y0)
                    bx1 = max(bx1, x1)
                    by1 = max(by1, y1)

                    flags = int(span.get("flags", 0))
                    weight = float(len(text.strip())) or 1.0
                    ssize = round(float(span.get("size", 12.0)), 2)
                    size_w[ssize] = size_w.get(ssize, 0.0) + weight
                    fname = str(span.get("font", "Helvetica"))
                    font_w[fname] = font_w.get(fname, 0.0) + weight
                    is_bold = bool(flags & _FLAG_BOLD)
                    bold_w[is_bold] = bold_w.get(is_bold, 0.0) + weight
                    is_italic = bool(flags & _FLAG_ITALIC)
                    italic_w[is_italic] = italic_w.get(is_italic, 0.0) + weight
                    chex = _color_to_hex(span.get("color"))
                    color_w[chex] = color_w.get(chex, 0.0) + weight

                    span_texts.append(text)

                if span_texts:
                    line_texts.append("".join(span_texts).rstrip())

            if not line_texts or bx0 == float("inf"):
                continue

            raw.append(
                {
                    "lines": line_texts,
                    "x0": bx0,
                    "y0": by0,
                    "x1": bx1,
                    "y1": by1,
                    "size_w": size_w,
                    "font_w": font_w,
                    "bold_w": bold_w,
                    "italic_w": italic_w,
                    "color_w": color_w,
                }
            )

        # Phase 2: stitch stacked lines back into paragraphs. Two records join
        # when they share a left edge and font size and the vertical gap between
        # them is no bigger than a line — i.e. they read as one block of text.
        # Left-edge alignment keeps the CV's two columns (and headings, which
        # differ in size) separate.
        raw.sort(key=lambda r: (round(r["y0"], 1), round(r["x0"], 1)))
        merged: List[Dict[str, Any]] = []

        for r in raw:
            r_size = float(_dominant(r["size_w"]) or 12.0)
            r_color = str(_dominant(r["color_w"]) or "#111111")
            target = None
            for p in reversed(merged):
                if abs(p["x0"] - r["x0"]) > _COL_TOL:
                    continue
                p_size = float(_dominant(p["size_w"]) or 12.0)
                if abs(p_size - r_size) > _SIZE_TOL:
                    continue
                if str(_dominant(p["color_w"]) or "#111111") != r_color:
                    continue
                gap = r["y0"] - p["y1"]
                if gap > _GAP_FACTOR * r_size or gap < -_OVERLAP_FACTOR * r_size:
                    continue
                target = p
                break

            if target is None:
                merged.append(r)
                continue

            target["lines"].extend(r["lines"])
            target["x0"] = min(target["x0"], r["x0"])
            target["y0"] = min(target["y0"], r["y0"])
            target["x1"] = max(target["x1"], r["x1"])
            target["y1"] = max(target["y1"], r["y1"])
            _merge_counter(target["size_w"], r["size_w"])
            _merge_counter(target["font_w"], r["font_w"])
            _merge_counter(target["bold_w"], r["bold_w"])
            _merge_counter(target["italic_w"], r["italic_w"])
            _merge_counter(target["color_w"], r["color_w"])

        blocks: List[Dict[str, Any]] = []
        for seq, p in enumerate(merged):
            text = "\n".join(p["lines"]).strip()
            if not text:
                continue
            blocks.append(
                {
                    "id": f"blk_{page}_{seq}",
                    "x": round(p["x0"] * scale, 2),
                    "y": round(p["y0"] * scale, 2),
                    "w": round((p["x1"] - p["x0"]) * scale, 2),
                    "h": round((p["y1"] - p["y0"]) * scale, 2),
                    "text": text,
                    "fontSize": round(float(_dominant(p["size_w"]) or 12.0) * scale, 2),
                    "fontName": str(_dominant(p["font_w"]) or "Helvetica"),
                    "bold": bool(_dominant(p["bold_w"]) or False),
                    "italic": bool(_dominant(p["italic_w"]) or False),
                    "color": str(_dominant(p["color_w"]) or "#111111"),
                }
            )

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
