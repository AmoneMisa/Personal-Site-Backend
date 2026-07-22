from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF (pulled in transitively by pdf2docx)

# ---------------------------------------------------------------------------
# Fonts
#
# The runtime image (python:3.13-slim) ships no system fonts, and reportlab's
# built-in Base-14 fonts are Latin-only — useless for the Cyrillic content this
# app deals with. We install `fonts-dejavu-core` in the Dockerfile and embed the
# matching DejaVu face when re-typesetting edited text, so the output PDF is
# self-contained and renders Cyrillic/Latin correctly. If the font files are
# somehow missing we fall back to PyMuPDF's Base-14 "helv" (Latin only) rather
# than crash.
# ---------------------------------------------------------------------------
_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
)

# class -> (regular, bold, italic, bold-italic)
_DEJAVU: Dict[str, Tuple[str, str, str, str]] = {
    "sans": (
        "DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
        "DejaVuSans-Oblique.ttf",
        "DejaVuSans-BoldOblique.ttf",
    ),
    "serif": (
        "DejaVuSerif.ttf",
        "DejaVuSerif-Bold.ttf",
        "DejaVuSerif-Italic.ttf",
        "DejaVuSerif-BoldItalic.ttf",
    ),
    "mono": (
        "DejaVuSansMono.ttf",
        "DejaVuSansMono-Bold.ttf",
        "DejaVuSansMono-Oblique.ttf",
        "DejaVuSansMono-BoldOblique.ttf",
    ),
}

_SERIF_HINTS = ("times", "serif", "georgia", "garamond", "minion", "roman", "book antiqua")
_MONO_HINTS = ("courier", "mono", "consol", "menlo", "code")

_ALIGN = {"left": 0, "center": 1, "right": 2, "justify": 3}


def _classify(font_name: str) -> str:
    f = (font_name or "").lower()
    if any(h in f for h in _MONO_HINTS):
        return "mono"
    if any(h in f for h in _SERIF_HINTS):
        return "serif"
    return "sans"


def _find_font_file(cls: str, bold: bool, italic: bool) -> Optional[str]:
    reg, b, i, bi = _DEJAVU.get(cls, _DEJAVU["sans"])
    if bold and italic:
        name = bi
    elif bold:
        name = b
    elif italic:
        name = i
    else:
        name = reg

    for d in _FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    # style variant missing -> fall back to the regular face of the same class
    for d in _FONT_DIRS:
        p = os.path.join(d, reg)
        if os.path.exists(p):
            return p
    return None


def _hex_to_rgb01(s: str) -> Tuple[float, float, float]:
    v = (s or "").strip().lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    if len(v) != 6:
        return (0.07, 0.07, 0.07)
    try:
        return (int(v[0:2], 16) / 255.0, int(v[2:4], 16) / 255.0, int(v[4:6], 16) / 255.0)
    except Exception:
        return (0.07, 0.07, 0.07)


def _clamp01(n: float) -> float:
    return max(0.0, min(1.0, n))


def _redact_originals(page: "fitz.Page", blocks: List[Dict[str, Any]]) -> bool:
    """Queue redactions for the original extracted regions. Returns True if any."""
    any_marked = False
    for blk in blocks:
        orig = blk.get("orig")
        if not orig:
            continue
        x, y, w, h = orig.get("xPt"), orig.get("yPt"), orig.get("wPt"), orig.get("hPt")
        if None in (x, y, w, h):
            continue
        pad = 0.5  # cover glyph anti-aliasing at the region edges
        rect = fitz.Rect(x - pad, y - pad, x + w + pad, y + h + pad)
        # cross_out=False -> just clear+fill the area (no diagonal lines)
        page.add_redact_annot(rect, fill=(1, 1, 1), cross_out=False)
        any_marked = True
    return any_marked


def _apply_redactions(page: "fitz.Page") -> None:
    # Only remove text; leave images/vector graphics intact where supported.
    try:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
    except (TypeError, AttributeError):
        page.apply_redactions()


def _insert_block_text(page: "fitz.Page", blk: Dict[str, Any]) -> None:
    text = str(blk.get("text") or "")
    if not text.strip():
        return

    x, y, w, h = blk.get("xPt"), blk.get("yPt"), blk.get("wPt"), blk.get("hPt")
    if None in (x, y, w, h):
        return

    fs = float(blk.get("fontSizePt") or 12.0)
    if fs <= 0:
        fs = 12.0

    bold = bool(blk.get("bold"))
    italic = bool(blk.get("italic"))
    cls = _classify(str(blk.get("fontName") or ""))
    fontfile = _find_font_file(cls, bold, italic)
    color = _hex_to_rgb01(str(blk.get("color") or "#111111"))
    align = _ALIGN.get(str(blk.get("align") or "left").lower(), 0)

    try:
        opacity = _clamp01(float(blk.get("opacity", 1.0)))
    except Exception:
        opacity = 1.0

    kwargs: Dict[str, Any] = dict(
        fontsize=fs,
        color=color,
        align=align,
        fill_opacity=opacity,
    )
    if fontfile:
        kwargs["fontname"] = "F0"
        kwargs["fontfile"] = fontfile
    else:
        kwargs["fontname"] = "helv"  # Base-14 fallback (Latin only)

    # give the box a little vertical slack so a single line isn't clipped
    rect = fitz.Rect(x, y, x + max(w, fs), y + max(h, fs * 1.4))
    rc = page.insert_textbox(rect, text, **kwargs)

    # insert_textbox returns the unused height; negative means it overflowed.
    if rc < 0:
        grow = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1 + (-rc) + fs)
        page.insert_textbox(grow, text, **kwargs)


def apply_text_edits(src_pdf: str, out_pdf: str, text_edits: Dict[int, List[Dict[str, Any]]]) -> None:
    """
    Re-typeset edited PDF text.

    For each page: truly redact the original extracted text regions (so the old
    glyphs are removed, not merely covered), then draw the edited text on top
    using an embedded DejaVu face mapped from the requested family/style.

    Geometry uses the frontend's DPI-independent PDF points (`*Pt`, top-left
    origin), which line up 1:1 with PyMuPDF's page coordinate system.
    """
    doc = fitz.open(src_pdf)
    try:
        for page_no, blocks in text_edits.items():
            if not blocks:
                continue
            idx = int(page_no) - 1
            if idx < 0 or idx >= doc.page_count:
                continue

            page = doc.load_page(idx)

            if _redact_originals(page, blocks):
                _apply_redactions(page)

            for blk in blocks:
                _insert_block_text(page, blk)

        doc.save(out_pdf, garbage=3, deflate=True)
    finally:
        doc.close()


def apply_links(src_pdf: str, out_pdf: str, links: Dict[int, List[Dict[str, Any]]]) -> None:
    """
    Attach clickable URI link annotations to an already-rendered PDF.

    Runs as the final save stage (after raster overlays) so the annotations are
    not stripped by earlier passes. Geometry is the frontend's DPI-independent
    PDF points (`*Pt`, top-left origin), matching PyMuPDF's coordinate system.
    Editing `src_pdf == out_pdf` in place is supported.
    """
    tmp = f"{out_pdf}.links.tmp"
    doc = fitz.open(src_pdf)
    try:
        for page_no, page_links in links.items():
            if not page_links:
                continue
            idx = int(page_no) - 1
            if idx < 0 or idx >= doc.page_count:
                continue

            page = doc.load_page(idx)
            for ln in page_links:
                uri = str(ln.get("uri") or "").strip()
                x, y, w, h = ln.get("xPt"), ln.get("yPt"), ln.get("wPt"), ln.get("hPt")
                if not uri or None in (x, y, w, h):
                    continue
                if w <= 0 or h <= 0:
                    continue
                rect = fitz.Rect(x, y, x + w, y + h)
                page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": uri})

        # save to a temp file and swap, so in-place (src == out) saves are safe
        doc.save(tmp, garbage=3, deflate=True)
    finally:
        doc.close()

    os.replace(tmp, out_pdf)
