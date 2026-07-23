from __future__ import annotations

import io
import math
import os
from typing import Any, Dict, List

import fitz  # PyMuPDF (pulled in transitively by pdf2docx)

try:
    from PIL import Image, ImageDraw  # Pillow (declared in requirements)
except Exception:  # pragma: no cover - Pillow is expected in the runtime image
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore


def _detect_circular_clip(page: "fitz.Page", bbox) -> tuple | None:
    """
    If an image placement is clipped to a circle/ellipse in the PDF, return that
    clip as fractions of the image bbox: (cx, cy, rx, ry) each in 0..1.

    Design tools (Canva/Figma/Word) round a photo with a vector clip path made of
    Bézier curves rather than baking roundness into the raster, so the extracted
    image is a full rectangle. We recover the intended shape by matching a curved
    clip that is concentric with the placement and no larger than it (which also
    excludes the surrounding ring, a wider concentric circle). Fractions keep the
    result independent of DPI / display scale.
    """
    ix0, iy0, ix1, iy1 = bbox
    iw, ih = ix1 - ix0, iy1 - iy0
    if iw <= 1 or ih <= 1:
        return None
    icx, icy = (ix0 + ix1) / 2.0, (iy0 + iy1) / 2.0

    try:
        drawings = page.get_drawings(extended=True)
    except TypeError:
        return None
    except Exception:
        return None

    best = None
    for d in drawings:
        if d.get("type") != "clip":
            continue
        # curves ('c') mark a rounded clip; purely rectangular clips are 're'
        if not any(it and it[0] == "c" for it in d.get("items", [])):
            continue
        r = d.get("rect") or d.get("scissor")
        if r is None:
            continue
        r = fitz.Rect(r)
        cw, ch = r.width, r.height
        if cw <= 1 or ch <= 1:
            continue
        # concentric with the placement, and not bigger than it (skip the ring)
        if abs((r.x0 + r.x1) / 2.0 - icx) > 4.0 or abs((r.y0 + r.y1) / 2.0 - icy) > 4.0:
            continue
        if cw > iw * 1.02 or ch > ih * 1.02:
            continue
        area = cw * ch
        if best is None or area > best[0]:
            best = (area, r)

    if best is None:
        return None
    r = best[1]
    return (
        ((r.x0 + r.x1) / 2.0 - ix0) / iw,
        ((r.y0 + r.y1) / 2.0 - iy0) / ih,
        (r.width / 2.0) / iw,
        (r.height / 2.0) / ih,
    )


def _bake_ellipse_alpha(path: str, frac: tuple) -> None:
    """Punch an elliptical alpha hole into the PNG at `path` (in place), so the
    saved raster is transparent outside the detected circular clip."""
    if Image is None or ImageDraw is None:
        return
    try:
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        fcx, fcy, frx, fry = frac
        cx, cy, rx, ry = fcx * w, fcy * h, frx * w, fry * h
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
        img.putalpha(mask)
        img.save(path)
    except Exception:
        # leave the rectangular raster as-is if masking fails
        pass


def extract_images(src_pdf: str, page: int, dpi: int, out_dir: str) -> List[Dict[str, Any]]:
    """
    Extract each embedded raster image placed on a 1-based page.

    Every placement becomes one item with its bounding box in the rendered-PNG
    pixel space at `dpi` (top-left origin, matching the preview the frontend
    overlays) plus a `name` referencing the extracted PNG written into `out_dir`.
    The frontend loads that PNG as a movable/resizable canvas object; the raw
    bytes are kept so the exporter can re-insert the image at full resolution.

    Files are keyed by xref so repeated placements of the same image share a
    single decoded PNG on disk.
    """
    scale = (dpi if dpi > 0 else 72) / 72.0

    doc = fitz.open(src_pdf)
    try:
        idx = page - 1
        if idx < 0 or idx >= doc.page_count:
            return []

        pg = doc.load_page(idx)
        os.makedirs(out_dir, exist_ok=True)

        items: List[Dict[str, Any]] = []
        seq = 0

        # get_image_info(xrefs=True) -> one entry per placement, with the on-page
        # bbox (page coordinates == top-left points) and the image's xref.
        for info in pg.get_image_info(xrefs=True):
            xref = int(info.get("xref", 0) or 0)
            bbox = info.get("bbox")
            if xref <= 0 or not bbox:
                continue

            x0, y0, x1, y1 = bbox
            w = x1 - x0
            h = y1 - y0
            if w <= 1 or h <= 1:
                continue

            # circular/elliptical clip applied to this placement, if any
            clip_frac = _detect_circular_clip(pg, bbox)

            name = f"p{page}_x{xref}.png"
            path = os.path.join(out_dir, name)
            if not os.path.exists(path):
                try:
                    pix = fitz.Pixmap(doc, xref)
                    # CMYK / other multi-channel -> convert to RGB before PNG save
                    if pix.n - pix.alpha >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)

                    # Apply the image's soft-mask (alpha channel) if present, so
                    # circular / feathered / transparent clips survive instead of
                    # being flattened to an opaque rectangle. The smask is a
                    # separate grayscale xref referenced by the base image.
                    try:
                        smask = int(doc.extract_image(xref).get("smask", 0) or 0)
                    except Exception:
                        smask = 0
                    if smask > 0:
                        try:
                            mask = fitz.Pixmap(doc, smask)
                            pix = fitz.Pixmap(pix, mask)
                        except Exception:
                            pass

                    pix.save(path)
                    pix = None

                    # Re-apply the vector circle as raster alpha so the extracted
                    # object matches the page (and re-inserts round on export).
                    if clip_frac is not None:
                        _bake_ellipse_alpha(path, clip_frac)
                except Exception:
                    continue

            items.append(
                {
                    "id": f"img_{page}_{seq}",
                    "name": name,
                    "x": round(x0 * scale, 2),
                    "y": round(y0 * scale, 2),
                    "w": round(w * scale, 2),
                    "h": round(h * scale, 2),
                }
            )
            seq += 1

        return items
    finally:
        doc.close()


def _redact_image_regions(page: "fitz.Page", rects: List["fitz.Rect"]) -> None:
    """Remove the original image pixels under `rects`, preserving text/vector."""
    if not rects:
        return
    for rect in rects:
        page.add_redact_annot(rect)

    # Prefer removing ONLY images so text/graphics behind or beside the picture
    # survive; fall back progressively on older PyMuPDF builds.
    try:
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_REMOVE,
            graphics=fitz.PDF_REDACT_GRAPHICS_NONE,
            text=fitz.PDF_REDACT_TEXT_NONE,
        )
    except (TypeError, AttributeError):
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE)
        except (TypeError, AttributeError):
            page.apply_redactions()


def _rotated_png(path: str, angle_deg: float) -> bytes:
    """Return `path` rotated clockwise by `angle_deg`, expanded, as PNG bytes."""
    # Fabric angles are clockwise; PIL rotates counter-clockwise -> negate.
    img = Image.open(path).convert("RGBA")
    rot = img.rotate(-angle_deg, expand=True, resample=Image.BICUBIC)
    buf = io.BytesIO()
    rot.save(buf, format="PNG")
    return buf.getvalue()


def _insert_edit(page: "fitz.Page", edit: Dict[str, Any], assets_dir: str) -> None:
    name = os.path.basename(str(edit.get("name") or ""))
    if not name:
        return
    path = os.path.join(assets_dir, name)
    if not os.path.exists(path):
        return

    x, y, w, h = edit.get("xPt"), edit.get("yPt"), edit.get("wPt"), edit.get("hPt")
    if None in (x, y, w, h) or w <= 0 or h <= 0:
        return

    angle = float(edit.get("angle") or 0.0) % 360.0

    if abs(angle) < 0.01:
        rect = fitz.Rect(x, y, x + w, y + h)
        page.insert_image(rect, filename=path, keep_proportion=False)
        return

    if Image is not None:
        # Pre-rotate the raster and drop it into the axis-aligned bounding box of
        # the rotated (unrotated w x h, centred) placement — matches Fabric, which
        # rotates about the object centre.
        rad = math.radians(angle)
        cx, cy = x + w / 2.0, y + h / 2.0
        aw = abs(w * math.cos(rad)) + abs(h * math.sin(rad))
        ah = abs(w * math.sin(rad)) + abs(h * math.cos(rad))
        rect = fitz.Rect(cx - aw / 2.0, cy - ah / 2.0, cx + aw / 2.0, cy + ah / 2.0)
        page.insert_image(rect, stream=_rotated_png(path, angle), keep_proportion=False)
        return

    # Pillow unavailable -> snap to the nearest right angle (insert_image only
    # accepts 0/90/180/270 for its own rotate parameter).
    r = int(round(angle / 90.0) * 90) % 360
    page.insert_image(fitz.Rect(x, y, x + w, y + h), filename=path, rotate=r, keep_proportion=False)


def apply_image_edits(
    src_pdf: str,
    out_pdf: str,
    image_edits: Dict[int, List[Dict[str, Any]]],
    assets_dir: str,
) -> None:
    """
    Re-place original images the user moved/resized/rotated/deleted.

    For each page: redact the originals of every touched or deleted image (so the
    old pixels are gone), then re-insert the non-deleted ones at their new
    geometry using the full-resolution bytes saved during extraction. Untouched
    images are never sent here, so they remain byte-for-byte in the source.

    Geometry is the frontend's DPI-independent PDF points (`*Pt`, top-left
    origin); `orig.*Pt` locates the source region to clear.
    """
    doc = fitz.open(src_pdf)
    try:
        for page_no, edits in image_edits.items():
            if not edits:
                continue
            idx = int(page_no) - 1
            if idx < 0 or idx >= doc.page_count:
                continue

            page = doc.load_page(idx)

            rects: List["fitz.Rect"] = []
            for e in edits:
                orig = e.get("orig") or {}
                ox, oy, ow, oh = orig.get("xPt"), orig.get("yPt"), orig.get("wPt"), orig.get("hPt")
                if None in (ox, oy, ow, oh):
                    continue
                pad = 0.5  # cover anti-aliasing at the image edges
                rects.append(fitz.Rect(ox - pad, oy - pad, ox + ow + pad, oy + oh + pad))
            _redact_image_regions(page, rects)

            for e in edits:
                if e.get("deleted"):
                    continue
                _insert_edit(page, e, assets_dir)

        doc.save(out_pdf, garbage=3, deflate=True)
    finally:
        doc.close()
