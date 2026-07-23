from __future__ import annotations

import re
from typing import Any, Dict, List

import fitz  # PyMuPDF (pulled in transitively by pdf2docx)

# PyMuPDF span "flags" bitmask (see get_text("dict") docs)
_FLAG_ITALIC = 1 << 1  # 2
_FLAG_BOLD = 1 << 4  # 16

# Many PDFs encode weight/slant in the font name rather than the flags bitmask
# (e.g. "Now-Black", "Aileron-Bold", "Lato-Italic"), so detect those too.
_BOLD_NAMES = ("bold", "black", "heavy", "semibold", "demibold", "extrabold", "ultra")
_ITALIC_NAMES = ("italic", "oblique")


def _span_bold(span: Dict[str, Any]) -> bool:
    if int(span.get("flags", 0)) & _FLAG_BOLD:
        return True
    return any(k in str(span.get("font", "")).lower() for k in _BOLD_NAMES)


def _span_italic(span: Dict[str, Any]) -> bool:
    if int(span.get("flags", 0)) & _FLAG_ITALIC:
        return True
    return any(k in str(span.get("font", "")).lower() for k in _ITALIC_NAMES)

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


def _counters_from_lines(lines: List[Dict[str, Any]]):
    """Rebuild the char-weighted style tallies for a subset of a block's lines
    (used after re-splitting a merged block into uniform-pitch pieces)."""
    size_w: Dict[float, float] = {}
    font_w: Dict[str, float] = {}
    bold_w: Dict[bool, float] = {}
    italic_w: Dict[bool, float] = {}
    color_w: Dict[str, float] = {}
    for ln in lines:
        for run in ln["runs"]:
            w = float(run["n"]) or 1.0
            size_w[run["size"]] = size_w.get(run["size"], 0.0) + w
            font_w[run["font"]] = font_w.get(run["font"], 0.0) + w
            bold_w[run["bold"]] = bold_w.get(run["bold"], 0.0) + w
            italic_w[run["italic"]] = italic_w.get(run["italic"], 0.0) + w
            color_w[run["color"]] = color_w.get(run["color"], 0.0) + w
    return size_w, font_w, bold_w, italic_w, color_w


def _split_uniform_pitch(lines: List[Dict[str, Any]], dom_size: float) -> List[List[Dict[str, Any]]]:
    """Split baseline-sorted lines into contiguous groups whose row-to-row
    pitch is uniform. Fabric renders a block with a single lineHeight, so a
    block whose internal spacing changes (e.g. a tight run of skills followed
    by a looser gap) can only be placed pixel-exact if it is broken where the
    pitch changes. A new group starts where the gap to the previous row differs
    from the group's established pitch by more than a quarter of the font."""
    ordered = sorted(lines, key=lambda ln: ln["y0"])
    tol = max(1.0, 0.25 * dom_size)
    groups: List[List[Dict[str, Any]]] = []
    pitch: float | None = None
    for ln in ordered:
        if not groups:
            groups.append([ln])
            pitch = None
            continue
        gap = ln["y0"] - groups[-1][-1]["y0"]
        if pitch is None:
            groups[-1].append(ln)
            pitch = gap
        elif abs(gap - pitch) <= tol:
            groups[-1].append(ln)
        else:
            groups.append([ln])
            pitch = None
    return groups


# Paragraph-merge tolerances, all in unscaled PDF points.
_COL_TOL = 3.0        # max left-edge drift for two lines to share a column
_SIZE_TOL = 1.5       # max font-size difference to treat lines as one paragraph
_GAP_FACTOR = 0.8     # max blank vertical gap as a fraction of the line's size
_OVERLAP_FACTOR = 0.4  # allow slight bbox overlap between stacked lines
_COL_GAP_FACTOR = 4.0  # horizontal gap (in font sizes) that marks a column break


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

            # collect every span with its style, grouped by the PyMuPDF line it
            # came from (a row *candidate*); rows are merged by baseline below.
            row_cands: List[Dict[str, Any]] = []
            for line in blk.get("lines", []):
                sps: List[Dict[str, Any]] = []
                ly0 = float("inf")
                ly1 = float("-inf")
                for span in line.get("spans", []):
                    text = span.get("text") or ""
                    if not text.strip():
                        continue

                    x0, y0, x1, y1 = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                    ly0 = min(ly0, y0)
                    ly1 = max(ly1, y1)

                    sps.append(
                        {
                            "text": text,
                            "x0": x0,
                            "x1": x1,
                            "size": round(float(span.get("size", 12.0)), 2),
                            "font": str(span.get("font", "Helvetica")),
                            "bold": _span_bold(span),
                            "italic": _span_italic(span),
                            "color": _color_to_hex(span.get("color")),
                        }
                    )
                if sps:
                    row_cands.append({"y0": ly0, "y1": ly1, "spans": sps})

            if not row_cands:
                continue

            # merge candidates whose vertical extents overlap into one visual row
            row_cands.sort(key=lambda r: r["y0"])
            rows: List[Dict[str, Any]] = []
            for rc in row_cands:
                dst = None
                for row in rows:
                    ov = min(row["y1"], rc["y1"]) - max(row["y0"], rc["y0"])
                    minh = min(row["y1"] - row["y0"], rc["y1"] - rc["y0"]) or 1.0
                    if ov > 0.5 * minh:
                        dst = row
                        break
                if dst is None:
                    rows.append({"y0": rc["y0"], "y1": rc["y1"], "spans": list(rc["spans"])})
                else:
                    dst["spans"].extend(rc["spans"])
                    dst["y0"] = min(dst["y0"], rc["y0"])
                    dst["y1"] = max(dst["y1"], rc["y1"])

            # split each visual row into column segments wherever a wide
            # horizontal gap separates neighbouring spans. A two-column layout
            # (e.g. a left-column URL and a right-column line sharing a baseline)
            # must not collapse into one line, while a title spanning a single
            # row ("MARHARYTA … KUBAI", gap ~3x font) stays together.
            segments: List[Dict[str, Any]] = []
            for row in rows:
                group: List[Dict[str, Any]] = []
                prev_x1 = None
                for s in sorted(row["spans"], key=lambda s: s["x0"]):
                    if (
                        prev_x1 is not None
                        and s["x0"] - prev_x1 > _COL_GAP_FACTOR * s["size"]
                        and group
                    ):
                        segments.append({"y0": row["y0"], "y1": row["y1"], "spans": group})
                        group = []
                    group.append(s)
                    prev_x1 = s["x1"]
                if group:
                    segments.append({"y0": row["y0"], "y1": row["y1"], "spans": group})

            # cluster segments into columns by horizontal overlap / left edge, so
            # each column becomes its own block with its own x-origin.
            columns: List[Dict[str, Any]] = []
            for seg in sorted(
                segments, key=lambda g: (g["y0"], min(s["x0"] for s in g["spans"]))
            ):
                sx0 = min(s["x0"] for s in seg["spans"])
                sx1 = max(s["x1"] for s in seg["spans"])
                dst = None
                best = 0.0
                for col in columns:
                    overlap = min(col["x1"], sx1) - max(col["x0"], sx0)
                    if overlap > best:
                        best = overlap
                        dst = col
                if dst is None:
                    for col in columns:
                        if abs(col["x0"] - sx0) <= _COL_TOL:
                            dst = col
                            break
                if dst is None:
                    columns.append({"x0": sx0, "x1": sx1, "segs": [seg]})
                else:
                    dst["x0"] = min(dst["x0"], sx0)
                    dst["x1"] = max(dst["x1"], sx1)
                    dst["segs"].append(seg)

            # one raw record per column: its rows stacked as lines with per-span
            # style runs, plus char-weighted style tallies for the dominant look.
            for col in sorted(columns, key=lambda c: c["x0"]):
                # split a column's stacked rows into groups wherever the font
                # size jumps, so a big title line and its smaller subtitle become
                # separate blocks (each rendered at its own size) instead of one
                # block collapsed to the char-weighted dominant size.
                seg_groups: List[List[Dict[str, Any]]] = []
                prev_size = None
                for seg in sorted(col["segs"], key=lambda g: g["y0"]):
                    seg_size = max(s["size"] for s in seg["spans"])
                    if seg_groups and prev_size is not None and abs(seg_size - prev_size) > _SIZE_TOL:
                        seg_groups.append([seg])
                    elif seg_groups:
                        seg_groups[-1].append(seg)
                    else:
                        seg_groups.append([seg])
                    prev_size = seg_size

                for seg_group in seg_groups:
                    lines: List[Dict[str, Any]] = []
                    cx0 = cy0 = float("inf")
                    cx1 = cy1 = float("-inf")
                    size_w: Dict[float, float] = {}
                    font_w: Dict[str, float] = {}
                    bold_w: Dict[bool, float] = {}
                    italic_w: Dict[bool, float] = {}
                    color_w: Dict[str, float] = {}
                    for seg in seg_group:
                        cur = ""
                        runs: List[Dict[str, Any]] = []
                        prev_x1 = None
                        for s in sorted(seg["spans"], key=lambda s: s["x0"]):
                            piece = s["text"]
                            # bridge an ordinary word-space gap between merged pieces
                            if (
                                prev_x1 is not None
                                and s["x0"] - prev_x1 > 0.3 * s["size"]
                                and not cur.endswith(" ")
                                and not piece.startswith(" ")
                            ):
                                cur += " "
                                runs.append({"n": 1, "bold": s["bold"], "italic": s["italic"],
                                             "color": s["color"], "size": s["size"], "font": s["font"]})
                            cur += piece
                            runs.append({"n": len(piece), "bold": s["bold"], "italic": s["italic"],
                                         "color": s["color"], "size": s["size"], "font": s["font"]})
                            prev_x1 = s["x1"]

                            cx0 = min(cx0, s["x0"])
                            cx1 = max(cx1, s["x1"])
                            cy0 = min(cy0, seg["y0"])
                            cy1 = max(cy1, seg["y1"])
                            w = float(len(s["text"].strip())) or 1.0
                            size_w[s["size"]] = size_w.get(s["size"], 0.0) + w
                            font_w[s["font"]] = font_w.get(s["font"], 0.0) + w
                            bold_w[s["bold"]] = bold_w.get(s["bold"], 0.0) + w
                            italic_w[s["italic"]] = italic_w.get(s["italic"], 0.0) + w
                            color_w[s["color"]] = color_w.get(s["color"], 0.0) + w

                        # rstrip trailing whitespace, trimming run lengths to stay aligned
                        trim = len(cur) - len(cur.rstrip())
                        cur = cur.rstrip()
                        while trim > 0 and runs:
                            if runs[-1]["n"] <= trim:
                                trim -= runs.pop()["n"]
                            else:
                                runs[-1]["n"] -= trim
                                trim = 0
                        if cur:
                            lines.append({
                                "text": cur,
                                "runs": runs,
                                "y0": seg["y0"],
                                "y1": seg["y1"],
                                "x0": min(s["x0"] for s in seg["spans"]),
                                "x1": max(s["x1"] for s in seg["spans"]),
                            })

                    if not lines or cx0 == float("inf"):
                        continue

                    raw.append(
                        {
                            "lines": lines,
                            "x0": cx0,
                            "y0": cy0,
                            "x1": cx1,
                            "y1": cy1,
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
        seq = 0
        for p in merged:
            # A Fabric block is stacked with one uniform lineHeight, so break a
            # merged paragraph wherever its row pitch changes and emit each
            # uniform-pitch run as its own block. This lets every line land at
            # its exact source position (pixel-perfect overlay) instead of
            # drifting when a block mixes tight and loose spacing.
            dom_size = float(_dominant(p["size_w"]) or 12.0)
            for group in _split_uniform_pitch(p["lines"], dom_size):
                text = "\n".join(ln["text"] for ln in group)
                if not text.strip():
                    continue

                size_w, font_w, bold_w, italic_w, color_w = _counters_from_lines(group)
                gx0 = min(ln["x0"] for ln in group)
                gy0 = min(ln["y0"] for ln in group)
                gx1 = max(ln["x1"] for ln in group)
                gy1 = max(ln["y1"] for ln in group)
                g_dom_size = float(_dominant(size_w) or 12.0)

                # true line pitch from the real baselines: the median gap
                # between consecutive rows divided by the font size. Emitting
                # this lets the editor space stacked lines exactly like the
                # source instead of estimating from the bbox height.
                ys = sorted(float(ln["y0"]) for ln in group)
                deltas = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.1]
                line_height = None
                if deltas and g_dom_size > 0:
                    deltas.sort()
                    median = deltas[len(deltas) // 2]
                    line_height = round(median / g_dom_size, 3)

                # per-line, per-span style runs so the frontend can rebuild
                # Fabric's styles[line][char] map (bold surname next to a
                # regular one, a bold word inside a sentence, an italic role
                # line, etc.).
                line_runs = [
                    [
                        {
                            "n": int(run["n"]),
                            "bold": bool(run["bold"]),
                            "italic": bool(run["italic"]),
                            "color": run["color"],
                            "fontSize": round(float(run["size"]) * scale, 2),
                            "fontName": run["font"],
                        }
                        for run in ln["runs"]
                    ]
                    for ln in group
                ]
                blocks.append(
                    {
                        "id": f"blk_{page}_{seq}",
                        "x": round(gx0 * scale, 2),
                        "y": round(gy0 * scale, 2),
                        "w": round((gx1 - gx0) * scale, 2),
                        "h": round((gy1 - gy0) * scale, 2),
                        "text": text,
                        "fontSize": round(g_dom_size * scale, 2),
                        "fontName": str(_dominant(font_w) or "Helvetica"),
                        "bold": bool(_dominant(bold_w) or False),
                        "italic": bool(_dominant(italic_w) or False),
                        "color": str(_dominant(color_w) or "#111111"),
                        "lineRuns": line_runs,
                        **({"lineHeight": line_height} if line_height else {}),
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
