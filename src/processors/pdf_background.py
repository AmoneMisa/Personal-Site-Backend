from __future__ import annotations

import os

import fitz  # PyMuPDF (pulled in transitively by pdf2docx)


def render_background_png(src_pdf: str, out_png: str, page: int, dpi: int = 144) -> None:
    """
    Render a 1-based page with every extractable text span and embedded image
    REMOVED, leaving only the non-editable decoration (vector graphics, fills,
    lines). This is the clean canvas the editor draws the editable text/image
    objects onto, so nothing is shown twice.

    The removed set matches exactly what `extract_text_blocks` / `extract_images`
    turn into editable objects, so every visible element is represented once:
    either as decoration baked into this raster, or as an editable object on top.
    """
    if page < 1:
        raise ValueError("Invalid page number")

    doc = fitz.open(src_pdf)
    try:
        idx = page - 1
        if idx < 0 or idx >= doc.page_count:
            raise ValueError("Invalid page number")

        pg = doc.load_page(idx)

        # queue text spans for removal (fill=False -> don't paint over the area,
        # just drop the glyphs so the panel/background behind them shows through)
        data = pg.get_text("dict")
        for blk in data.get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    if not (span.get("text") or "").strip():
                        continue
                    x0, y0, x1, y1 = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    if x1 - x0 <= 0 or y1 - y0 <= 0:
                        continue
                    pad = 0.5
                    pg.add_redact_annot(
                        fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad),
                        fill=False,
                        cross_out=False,
                    )

        # queue embedded images for removal
        for info in pg.get_image_info(xrefs=True):
            bbox = info.get("bbox")
            if not bbox:
                continue
            x0, y0, x1, y1 = bbox
            if x1 - x0 <= 1 or y1 - y0 <= 1:
                continue
            pg.add_redact_annot(fitz.Rect(x0, y0, x1, y1), fill=False, cross_out=False)

        # remove text + images inside the queued rects; keep vector graphics
        try:
            pg.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_REMOVE,
                graphics=fitz.PDF_REDACT_GRAPHICS_NONE,
            )
        except (TypeError, AttributeError):
            pg.apply_redactions()

        mat = fitz.Matrix((dpi or 72) / 72.0, (dpi or 72) / 72.0)
        pix = pg.get_pixmap(matrix=mat, alpha=True)

        os.makedirs(os.path.dirname(out_png), exist_ok=True)
        pix.save(out_png)
    finally:
        doc.close()
