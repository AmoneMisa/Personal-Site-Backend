from __future__ import annotations

import base64
import json
import os
import shutil
import time
import uuid
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from pypdf import PdfReader
from pypdf.generic import NameObject
from redis.asyncio import Redis

from fastapi.concurrency import run_in_threadpool

from ..processors.pdf_ops import merge_pdfs  # (старый мердж оставляем)
from ..processors.pdf_ops_new import apply_png_overlays  # (новый рендер поверх)
from ..processors.pdf_preview import render_pdf_page_to_png
from ..processors.pdf_background import render_background_png
from ..processors.pdf_text import extract_text_blocks, extract_links
from ..processors.pdf_textedit import apply_text_edits, apply_links
from ..processors.pdf_image import extract_images, apply_image_edits
from ..utils.redis_client import get_redis

try:
    import magic  # python-magic
except Exception:
    magic = None  # type: ignore

router = APIRouter(prefix="/pdf", tags=["pdf"])

# ----------------------------
# Config
# ----------------------------
STORAGE_ROOT = os.getenv("PDF_STORAGE_ROOT", "/var/app/storage/pdf")

DRAFT_TTL_SECONDS = int(os.getenv("PDF_DRAFT_TTL_SECONDS", "86400"))  # 24h
RESULT_TTL_SECONDS = int(os.getenv("PDF_RESULT_TTL_SECONDS", "3600"))  # 1h

MAX_FILE_SIZE = int(os.getenv("PDF_MAX_FILE_SIZE", str(50 * 1024 * 1024)))  # 50MB
MAX_FILES = int(os.getenv("PDF_MAX_FILES", "10"))
MAX_PAGES = int(os.getenv("PDF_MAX_PAGES", "500"))


# ----------------------------
# Helpers
# ----------------------------
def now_ts() -> int:
    return int(time.time())


def ensure_storage_root():
    os.makedirs(STORAGE_ROOT, exist_ok=True)


def doc_folder(doc_id: str) -> str:
    return os.path.join(STORAGE_ROOT, doc_id)


def source_path(doc_id: str) -> str:
    return os.path.join(doc_folder(doc_id), "source.pdf")


def result_path(doc_id: str) -> str:
    return os.path.join(doc_folder(doc_id), "result.pdf")


def preview_folder(doc_id: str) -> str:
    return os.path.join(doc_folder(doc_id), "previews")


def preview_path(doc_id: str, page: int, dpi: int) -> str:
    return os.path.join(preview_folder(doc_id), f"p{page}_dpi{dpi}.png")


def extracted_folder(doc_id: str) -> str:
    return os.path.join(doc_folder(doc_id), "extracted")


def safe_filename(name: str, fallback: str) -> str:
    base = os.path.basename(name or "").strip()
    return base if base else fallback


def safe_remove_result_files(doc_id: str):
    try:
        rp = result_path(doc_id)
        if os.path.exists(rp):
            os.remove(rp)
    except:
        pass


def safe_remove_doc_folder(doc_id: str):
    try:
        shutil.rmtree(doc_folder(doc_id), ignore_errors=True)
    except Exception:
        pass


def validate_pdf_signature(path: str):
    with open(path, "rb") as f:
        head = f.read(5)
    if head != b"%PDF-":
        raise HTTPException(415, "Uploaded file is not a valid PDF (missing %PDF- header)")


def validate_pdf_mime(path: str):
    if magic is None:
        return
    mime = magic.from_file(path, mime=True)
    if mime != "application/pdf":
        raise HTTPException(415, f"Only PDF allowed (detected {mime})")


def validate_pages_limit(path: str):
    reader = PdfReader(path)
    pages = len(reader.pages)
    if pages > MAX_PAGES:
        raise HTTPException(413, f"Max pages is {MAX_PAGES}")


async def save_upload_validated(upload: UploadFile, dest_path: str, max_size: int) -> int:
    written = 0
    with open(dest_path, "wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_size:
                raise HTTPException(413, f"Max file size is {max_size} bytes")
            out.write(chunk)
    return written


def _safe_page_box(page):
    box = None
    try:
        box = page.get(NameObject("/CropBox"))
    except Exception:
        box = None
    if box is None:
        try:
            box = page.get(NameObject("/MediaBox"))
        except Exception:
            box = None

    if box is None:
        return 595.0, 842.0

    try:
        arr = list(box)
        if len(arr) != 4:
            return 595.0, 842.0
        x0, y0, x1, y1 = [float(v) for v in arr]
        w = abs(x1 - x0)
        h = abs(y1 - y0)
        if w <= 0 or h <= 0:
            return 595.0, 842.0
        return w, h
    except Exception:
        return 595.0, 842.0


def _safe_pdf_num_pages(path: str) -> int:
    try:
        reader = PdfReader(path)
        return len(reader.pages)
    except Exception:
        return 0


def k_doc(doc_id: str) -> str:
    return f"pdf:doc:{doc_id}"


def k_draft(doc_id: str) -> str:
    return f"pdf:draft:{doc_id}"


def k_result(doc_id: str) -> str:
    return f"pdf:result:{doc_id}"


async def ensure_doc_exists(r: Redis, doc_id: str):
    raw = await r.get(k_doc(doc_id))
    if not raw:
        raise HTTPException(404, "Document not found or expired")


# ----------------------------
# Schemas
# ----------------------------
class CreateResp(BaseModel):
    docId: str
    expiresAtDraft: int


class DraftPutBody(BaseModel):
    draft: Dict[str, Any]


class OrigBlockModel(BaseModel):
    # DPI-independent PDF points (top-left origin) of the original extracted region
    xPt: Optional[float] = None
    yPt: Optional[float] = None
    wPt: Optional[float] = None
    hPt: Optional[float] = None

    model_config = {"extra": "ignore"}


class TextEditBlockModel(BaseModel):
    text: str = ""
    # edited block geometry in PDF points (top-left origin)
    xPt: Optional[float] = None
    yPt: Optional[float] = None
    wPt: Optional[float] = None
    hPt: Optional[float] = None
    fontSizePt: Optional[float] = None
    fontName: str = "Helvetica"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    align: str = "left"
    color: str = "#111111"
    opacity: float = 1.0
    angle: float = 0.0
    orig: Optional[OrigBlockModel] = None

    model_config = {"extra": "ignore"}


class PageSizeModel(BaseModel):
    widthPt: Optional[float] = None
    heightPt: Optional[float] = None

    model_config = {"extra": "ignore"}


class LinkModel(BaseModel):
    # clickable region geometry in PDF points (top-left origin) + target URI
    xPt: Optional[float] = None
    yPt: Optional[float] = None
    wPt: Optional[float] = None
    hPt: Optional[float] = None
    uri: str = ""

    model_config = {"extra": "ignore"}


class ImageEditModel(BaseModel):
    # which extracted image file this edit refers to
    name: str = ""
    # new placement in PDF points (top-left origin, unrotated box) + rotation
    xPt: Optional[float] = None
    yPt: Optional[float] = None
    wPt: Optional[float] = None
    hPt: Optional[float] = None
    angle: float = 0.0
    deleted: bool = False
    # original extracted region to clear (redact) from the source
    orig: Optional[OrigBlockModel] = None

    model_config = {"extra": "ignore"}


class SaveBody(BaseModel):
    overlays: Dict[int, str] = {}  # page -> dataURL or base64 (raster drawings)
    textEdits: Dict[int, List[TextEditBlockModel]] = {}  # page -> edited text blocks
    imageEdits: Dict[int, List[ImageEditModel]] = {}  # page -> moved/resized/deleted images
    links: Dict[int, List[LinkModel]] = {}  # page -> clickable link regions
    dpi: int = Field(default=144, ge=72, le=220)
    page: Optional[PageSizeModel] = None
    coords: Optional[Dict[str, Any]] = None

    model_config = {"extra": "ignore"}


# ----------------------------
# Routes
# ----------------------------
@router.post("/create", response_model=CreateResp)
async def create(files: List[UploadFile] = File(...)):
    ensure_storage_root()

    if not files:
        raise HTTPException(400, "No files uploaded")
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"Max files is {MAX_FILES}")

    doc_id = str(uuid.uuid4())
    folder = doc_folder(doc_id)
    os.makedirs(folder, exist_ok=True)

    tmp_paths: List[str] = []
    try:
        for i, f in enumerate(files):
            tmp = os.path.join(folder, safe_filename(f.filename, f"upload_{i}.pdf"))
            await save_upload_validated(f, tmp, MAX_FILE_SIZE)
            validate_pdf_signature(tmp)
            validate_pdf_mime(tmp)
            validate_pages_limit(tmp)
            tmp_paths.append(tmp)
    finally:
        for f in files:
            try:
                await f.close()
            except Exception:
                pass

    out_src = source_path(doc_id)
    if len(tmp_paths) == 1:
        shutil.copyfile(tmp_paths[0], out_src)
    else:
        merge_pdfs(tmp_paths, out_src)

    r: Redis = get_redis()
    expires_draft = now_ts() + DRAFT_TTL_SECONDS
    await r.set(
        k_doc(doc_id),
        json.dumps({"docId": doc_id, "expiresAtDraft": expires_draft}),
        ex=DRAFT_TTL_SECONDS,
    )

    return CreateResp(docId=doc_id, expiresAtDraft=expires_draft)


@router.get("/download/{doc_id}")
async def download_source(doc_id: str):
    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    path = source_path(doc_id)
    if not os.path.exists(path):
        raise HTTPException(404, "Source PDF not found")

    return FileResponse(path, media_type="application/pdf", filename=f"pdf_{doc_id}_source.pdf")


@router.get("/page-info/{doc_id}")
async def page_info(doc_id: str):
    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    path = source_path(doc_id)
    if not os.path.exists(path):
        raise HTTPException(404, "Source PDF not found")

    reader = PdfReader(path)
    pages = len(reader.pages)

    w, h = (595.0, 842.0)
    if pages > 0:
        w, h = _safe_page_box(reader.pages[0])

    return JSONResponse({"pages": pages, "pageW": w, "pageH": h})


@router.get("/preview/{doc_id}/{page}")
async def preview(doc_id: str, page: int, dpi: int = 144):
    # clamp dpi
    if dpi < 72:
        dpi = 72
    if dpi > 220:
        dpi = 220

    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    src = source_path(doc_id)
    if not os.path.exists(src):
        raise HTTPException(404, "Source PDF not found")

    total = _safe_pdf_num_pages(src)
    if total <= 0:
        raise HTTPException(500, "Unable to read PDF")

    if page < 1 or page > total:
        raise HTTPException(400, "Invalid page number")

    os.makedirs(preview_folder(doc_id), exist_ok=True)
    out_png = preview_path(doc_id, page, dpi)

    if os.path.exists(out_png):
        return FileResponse(out_png, media_type="image/png")

    try:
        render_pdf_page_to_png(src, out_png, page=page, dpi=dpi)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to render preview: {e}")

    return FileResponse(out_png, media_type="image/png")


@router.get("/background/{doc_id}/{page}")
async def background(doc_id: str, page: int, dpi: int = 144):
    # clamp dpi to match the preview raster the client overlays
    if dpi < 72:
        dpi = 72
    if dpi > 220:
        dpi = 220

    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    src = source_path(doc_id)
    if not os.path.exists(src):
        raise HTTPException(404, "Source PDF not found")

    total = _safe_pdf_num_pages(src)
    if total <= 0:
        raise HTTPException(500, "Unable to read PDF")

    if page < 1 or page > total:
        raise HTTPException(400, "Invalid page number")

    os.makedirs(preview_folder(doc_id), exist_ok=True)
    out_png = os.path.join(preview_folder(doc_id), f"bg_p{page}_dpi{dpi}.png")

    if os.path.exists(out_png):
        return FileResponse(out_png, media_type="image/png")

    try:
        await run_in_threadpool(render_background_png, src, out_png, page, dpi)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to render background: {e}")

    return FileResponse(out_png, media_type="image/png")


@router.get("/text-blocks/{doc_id}/{page}")
async def text_blocks(doc_id: str, page: int, dpi: int = 144):
    # clamp dpi to match the preview raster the client overlays
    if dpi < 72:
        dpi = 72
    if dpi > 220:
        dpi = 220

    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    src = source_path(doc_id)
    if not os.path.exists(src):
        raise HTTPException(404, "Source PDF not found")

    total = _safe_pdf_num_pages(src)
    if total <= 0:
        raise HTTPException(500, "Unable to read PDF")

    if page < 1 or page > total:
        raise HTTPException(400, "Invalid page number")

    try:
        # extraction is sync CPU/IO work -> keep it off the event loop
        blocks = await run_in_threadpool(extract_text_blocks, src, page, dpi)
        links = await run_in_threadpool(extract_links, src, page, dpi)
        raw_images = await run_in_threadpool(extract_images, src, page, dpi, extracted_folder(doc_id))
    except Exception as e:
        raise HTTPException(500, f"Failed to extract text: {e}")

    # expose extracted image files via the image-serving route
    images = [
        {**img, "url": f"/pdf/image/{doc_id}/{img['name']}"}
        for img in raw_images
        if img.get("name")
    ]

    return JSONResponse({"blocks": blocks, "links": links, "images": images})


@router.get("/image/{doc_id}/{name}")
async def get_extracted_image(doc_id: str, name: str):
    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    safe = os.path.basename(name or "")
    if not safe:
        raise HTTPException(404, "Image not found")

    path = os.path.join(extracted_folder(doc_id), safe)
    if not os.path.exists(path):
        raise HTTPException(404, "Image not found")

    return FileResponse(path, media_type="image/png")


@router.get("/draft/{doc_id}")
async def get_draft(doc_id: str):
    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    raw = await r.get(k_draft(doc_id))
    if not raw:
        raise HTTPException(404, "Draft not found")
    return JSONResponse({"draft": json.loads(raw)})


@router.put("/draft/{doc_id}")
async def put_draft(doc_id: str, body: DraftPutBody):
    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    await r.set(k_draft(doc_id), json.dumps(body.draft), ex=DRAFT_TTL_SECONDS)
    return JSONResponse({"ok": True, "expiresAtDraft": now_ts() + DRAFT_TTL_SECONDS})


@router.post("/save/{doc_id}")
async def save(doc_id: str, body: SaveBody):
    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    src = source_path(doc_id)
    if not os.path.exists(src):
        raise HTTPException(404, "Source PDF not found (expired?)")

    # decode overlays (dataURL OR raw base64)
    overlays_bytes: Dict[int, bytes] = {}
    for p, data in body.overlays.items():
        if not data:
            continue
        if isinstance(data, str) and data.startswith("data:image"):
            b64 = data.split(",", 1)[1]
        else:
            b64 = data
        try:
            overlays_bytes[int(p)] = base64.b64decode(b64)
        except Exception:
            raise HTTPException(400, f"Bad base64 for page {p}")

    # collect edited text blocks (page -> list of plain dicts for the processor)
    text_edits: Dict[int, List[Dict[str, Any]]] = {
        int(p): [b.model_dump() for b in blocks]
        for p, blocks in body.textEdits.items()
        if blocks
    }

    # collect moved/resized/rotated/deleted original images (page -> list of dicts)
    image_edits: Dict[int, List[Dict[str, Any]]] = {
        int(p): [e.model_dump() for e in edits if e.name]
        for p, edits in body.imageEdits.items()
        if edits
    }

    # collect clickable link regions (page -> list of {xPt,yPt,wPt,hPt,uri})
    links: Dict[int, List[Dict[str, Any]]] = {
        int(p): [ln.model_dump() for ln in page_links if ln.uri]
        for p, page_links in body.links.items()
        if page_links
    }

    out = result_path(doc_id)
    os.makedirs(doc_folder(doc_id), exist_ok=True)

    # Render pipeline (heavy sync work -> keep off the event loop):
    #   1) redact original text + re-typeset edited text (fitz)
    #   2) redact moved/deleted original images + re-insert them (fitz)
    #   3) stamp raster drawings (pen/shapes/added text) on top (reportlab)
    #   4) attach URI link annotations on the final PDF (fitz)
    def _render() -> None:
        work_src = src
        tmp_text = os.path.join(doc_folder(doc_id), "with_text.pdf")
        tmp_img = os.path.join(doc_folder(doc_id), "with_images.pdf")
        tmps: List[str] = []
        try:
            if text_edits:
                apply_text_edits(work_src, tmp_text, text_edits)
                work_src = tmp_text
                tmps.append(tmp_text)
            if image_edits:
                apply_image_edits(work_src, tmp_img, image_edits, extracted_folder(doc_id))
                work_src = tmp_img
                tmps.append(tmp_img)
            apply_png_overlays(work_src, out, overlays_bytes, dpi=body.dpi)
        finally:
            for tp in tmps:
                if os.path.exists(tp):
                    try:
                        os.remove(tp)
                    except Exception:
                        pass

        # links are added last so they survive all prior stages
        if links:
            apply_links(out, out, links)

    await run_in_threadpool(_render)

    # result TTL
    expires_result = now_ts() + RESULT_TTL_SECONDS
    await r.set(k_result(doc_id), json.dumps({"expiresAtResult": expires_result}), ex=RESULT_TTL_SECONDS)

    # draft больше не нужен после сохранения
    await r.delete(k_draft(doc_id))

    return JSONResponse({"downloadUrl": f"/api/pdf/download-result/{doc_id}", "expiresAtResult": expires_result})


@router.get("/download-result/{doc_id}")
async def download_result(doc_id: str):
    r: Redis = get_redis()

    raw = await r.get(k_result(doc_id))
    if not raw:
        # result истёк → удаляем ТОЛЬКО result.pdf
        safe_remove_result_files(doc_id)
        raise HTTPException(404, "Result not found or expired")

    path = result_path(doc_id)
    if not os.path.exists(path):
        raise HTTPException(404, "Result file missing")

    return FileResponse(path, media_type="application/pdf", filename=f"pdf_{doc_id}.pdf")


@router.delete("/{doc_id}")
async def delete_doc(doc_id: str):
    r: Redis = get_redis()
    await r.delete(k_draft(doc_id))
    await r.delete(k_result(doc_id))
    await r.delete(k_doc(doc_id))
    safe_remove_doc_folder(doc_id)
    return JSONResponse({"ok": True})


MAX_IMAGE_SIZE = int(os.getenv("PDF_MAX_IMAGE_SIZE", str(5 * 1024 * 1024)))  # 5MB
ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp"}


def assets_folder(doc_id: str) -> str:
    return os.path.join(doc_folder(doc_id), "assets")


def asset_path(doc_id: str, asset_id: str, ext: str) -> str:
    return os.path.join(assets_folder(doc_id), f"{asset_id}.{ext}")


def _ext_from_mime(mime: str) -> str:
    if mime == "image/png":
        return "png"
    if mime == "image/jpeg":
        return "jpg"
    if mime == "image/webp":
        return "webp"
    return "bin"


@router.post("/assets/{doc_id}")
async def upload_asset(doc_id: str, file: UploadFile = File(...)):
    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    if not file:
        raise HTTPException(400, "No file")

    # validate mime (best-effort)
    ct = (file.content_type or "").lower().strip()
    if ct and ct not in ALLOWED_IMAGE_MIME:
        raise HTTPException(415, f"Unsupported image type: {ct}")

    os.makedirs(assets_folder(doc_id), exist_ok=True)

    asset_id = uuid.uuid4().hex
    ext = _ext_from_mime(ct) if ct else "png"
    path = asset_path(doc_id, asset_id, ext)

    try:
        await save_upload_validated(file, path, MAX_IMAGE_SIZE)
    finally:
        try:
            await file.close()
        except Exception:
            pass

    return JSONResponse(
        {
            "assetId": asset_id,
            "url": f"/api/pdf/assets/{doc_id}/{asset_id}",
        }
    )


@router.get("/assets/{doc_id}/{asset_id}")
async def get_asset(doc_id: str, asset_id: str):
    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    folder = assets_folder(doc_id)
    if not os.path.isdir(folder):
        raise HTTPException(404, "Asset not found")

    # asset could be png/jpg/webp — найдём по префиксу
    for ext, mt in (("png", "image/png"), ("jpg", "image/jpeg"), ("webp", "image/webp")):
        p = asset_path(doc_id, asset_id, ext)
        if os.path.exists(p):
            return FileResponse(p, media_type=mt)

    raise HTTPException(404, "Asset not found")


@router.delete("/assets/{doc_id}/{asset_id}")
async def delete_asset(doc_id: str, asset_id: str):
    r: Redis = get_redis()
    await ensure_doc_exists(r, doc_id)

    for ext in ("png", "jpg", "webp"):
        p = asset_path(doc_id, asset_id, ext)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
            return JSONResponse({"ok": True})

    return JSONResponse({"ok": True})


import asyncio


async def pdf_storage_cleanup_loop():
    while True:
        try:
            r: Redis = get_redis()
            if os.path.isdir(STORAGE_ROOT):
                for doc_id in os.listdir(STORAGE_ROOT):
                    folder = doc_folder(doc_id)
                    if not os.path.isdir(folder):
                        continue
                    try:
                        exists = await r.exists(k_doc(doc_id))
                    except Exception:
                        exists = 1  # если Redis временно недоступен — не удаляем
                    if not exists:
                        safe_remove_doc_folder(doc_id)
        except Exception:
            pass

        await asyncio.sleep(30 * 60)  # 30 minutes
