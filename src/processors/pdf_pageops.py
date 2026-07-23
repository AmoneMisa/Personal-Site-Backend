from __future__ import annotations

import fitz  # PyMuPDF


def add_design_page(src_pdf: str, out_pdf: str, ref_index: int = 0) -> int:
    """
    Append a new page that reuses a reference page's *design* (the coloured
    column blocks and separator rules) but strips all content: text and every
    embedded image (which drops the avatar photo). The result is an empty
    themed page you can fill in the editor.

    Returns the 1-based number of the new page.
    """
    doc = fitz.open(src_pdf)
    try:
        n = doc.page_count
        if ref_index < 0 or ref_index >= n:
            ref_index = 0

        # independent full copy of the themed page, appended at the end
        doc.fullcopy_page(ref_index)
        page = doc[-1]

        # wipe text + images across the whole page, keep vector graphics so the
        # coloured sidebar / columns / rule lines survive
        page.add_redact_annot(page.rect)
        try:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_REMOVE,
                graphics=fitz.PDF_REDACT_GRAPHICS_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        except (TypeError, AttributeError):
            page.apply_redactions()

        doc.save(out_pdf, garbage=3, deflate=True)
        return doc.page_count
    finally:
        doc.close()
