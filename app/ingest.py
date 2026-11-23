"""Ingestion: file -> text segments + images -> chunks -> ICS tags -> embeddings.

Images (uploaded directly, or embedded in a PDF) are described once by the vision
model. That description is what gets embedded and searched, and the image itself is
shown next to any answer that uses it.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import uuid
from pathlib import Path

import numpy as np

from . import config, db, ics, providers, retrieval

TEXT_EXT = {".txt", ".md", ".csv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
DOC_EXT = {".pdf", ".docx"} | TEXT_EXT
ALLOWED_EXT = DOC_EXT | IMAGE_EXT
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
MAX_PDF_IMAGES = 24

IMAGE_PROMPT = (
    "You are indexing factory documentation so technicians can search it. Describe this image factually: "
    "what equipment or part it shows, what it is doing, and anything a technician would look for. "
    "Transcribe exactly any visible text, labels, tag numbers, codes, gauge readings and warning signs. "
    "If it is a diagram, explain what it shows. Do not guess details you cannot see. Under 180 words."
)

_queue_lock = asyncio.Lock()  # one document at a time: local models are easily saturated


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 150) -> list[str]:
    """Pack paragraphs into chunks of about max_chars, with a little overlap between them."""
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        cur = ""
        for sentence in re.split(r"(?<=[.!?])\s+|\n", para):
            while len(sentence) > max_chars:  # no sentence breaks at all
                pieces.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
            if cur and len(cur) + len(sentence) + 1 > max_chars:
                pieces.append(cur)
                cur = sentence
            else:
                cur = f"{cur} {sentence}".strip()
        if cur:
            pieces.append(cur)

    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 2 > max_chars:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap else ""
            tail = tail[tail.find(" ") + 1:] if " " in tail else ""
            cur = f"{tail}\n\n{piece}" if tail else piece
        else:
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def _parse_pdf(path: Path, doc_id: str) -> list[dict]:
    from PIL import Image
    from pypdf import PdfReader

    segments, seen, n_images = [], set(), 0
    reader = PdfReader(str(path))
    for page_no, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if text.strip():
            segments.append({"text": text, "page": page_no})
        if n_images >= MAX_PDF_IMAGES:
            continue
        try:
            images = list(page.images)
        except Exception:
            images = []
        for img in images:
            try:
                data = img.data
                digest = hashlib.sha1(data).hexdigest()
                if digest in seen:  # logos and headers repeat on every page
                    continue
                seen.add(digest)
                pil = Image.open(io.BytesIO(data))
                if min(pil.size) < 120:
                    continue
                out = config.FILES_DIR / f"{doc_id}_p{page_no}_{n_images}.png"
                pil.convert("RGB").save(out, "PNG")
                segments.append({"image_path": str(out), "page": page_no})
                n_images += 1
                if n_images >= MAX_PDF_IMAGES:
                    break
            except Exception:
                continue
    return segments


def _parse_docx(path: Path) -> list[dict]:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return [{"text": "\n\n".join(parts), "page": None}]


def parse(path: Path, doc_id: str) -> list[dict]:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return [{"image_path": str(path), "page": None}]
    if ext == ".pdf":
        return _parse_pdf(path, doc_id)
    if ext == ".docx":
        return _parse_docx(path)
    return [{"text": _decode(path.read_bytes()), "page": None}]


async def _describe(image_path: str, settings: dict) -> str:
    provider, model = providers.for_role("vision", settings)
    path = Path(image_path)
    mime = MIME.get(path.suffix.lower(), "image/png")
    return await providers.complete(
        provider, model, IMAGE_PROMPT,
        [{"role": "user", "content": "Describe this image.", "images": [(path.read_bytes(), mime)]}],
    )


async def embed_texts(texts: list[str], settings: dict) -> list[bytes]:
    provider, model = providers.for_role("embedding", settings)
    blobs: list[bytes] = []
    for i in range(0, len(texts), 32):
        for vec in await provider.embed(model, texts[i:i + 32]):
            arr = np.asarray(vec, dtype=np.float32)
            arr /= (np.linalg.norm(arr) or 1.0)
            blobs.append(arr.tobytes())
    return blobs


def create_document(filename: str, data: bytes) -> str:
    """Store the upload and register it as 'processing'. Returns the document id."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"Unsupported file type '{ext or filename}'.")
    doc_id = uuid.uuid4().hex[:12]
    path = config.FILES_DIR / f"{doc_id}{ext}"
    path.write_bytes(data)
    db.execute(
        "INSERT INTO documents(id, filename, kind, mime, size, path, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (doc_id, Path(filename).name, "image" if ext in IMAGE_EXT else "document",
         MIME.get(ext, "application/octet-stream"), len(data), str(path), "processing", db.now()),
    )
    return doc_id


async def process_document(doc_id: str, extra_codes: list[str] | None = None) -> None:
    async with _queue_lock:
        try:
            await _process(doc_id, extra_codes or [])
        except Exception as exc:  # surfaced in the Library view
            db.execute("UPDATE documents SET status='error', error=? WHERE id=?", (str(exc)[:500], doc_id))


async def _process(doc_id: str, extra_codes: list[str]) -> None:
    settings = config.load()
    doc = db.query("SELECT * FROM documents WHERE id=?", (doc_id,))[0]
    segments = await asyncio.to_thread(parse, Path(doc["path"]), doc_id)

    items: list[dict] = []
    for seg in segments:
        if seg.get("image_path"):
            caption = await _describe(seg["image_path"], settings)
            where = f" (page {seg['page']})" if seg["page"] else ""
            items.append({"text": f"Image from {doc['filename']}{where}: {caption}",
                          "page": seg["page"], "image_path": seg["image_path"]})
        else:
            for chunk in chunk_text(seg["text"]):
                items.append({"text": chunk, "page": seg["page"], "image_path": None})
    if not items:
        raise ValueError("No readable text or images found (scanned PDFs need OCR first).")

    # The file name gives each chunk context ("pump manual") that its own text may lack.
    blobs = await embed_texts([f"{doc['filename']}\n{it['text']}" for it in items], settings)
    # A manual names its part in the file name or title, not in every paragraph. Codes found there
    # (or given at upload) are applied to every chunk, so "the pump" passages stay linked to the part.
    title = next((s["text"] for s in segments if s.get("text")), "")[:300]
    doc_codes = list(dict.fromkeys(
        ics.find_codes(doc["filename"]) + ics.find_codes(title) + [c.strip() for c in extra_codes if c.strip()]))
    embed_model = config.embed_id(settings)

    with db.tx() as conn:
        for order, (item, blob) in enumerate(zip(items, blobs)):
            cur = conn.execute(
                "INSERT INTO chunks(doc_id, ord, text, page, image_path, embed_model, embedding) VALUES(?,?,?,?,?,?,?)",
                (doc_id, order, item["text"], item["page"], item["image_path"], embed_model, blob),
            )
            conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (cur.lastrowid, item["text"]))
            for code in dict.fromkeys(doc_codes + ics.find_codes(item["text"])):
                conn.execute("INSERT INTO chunk_codes(chunk_id, code) VALUES(?,?)", (cur.lastrowid, code))
        conn.execute("UPDATE documents SET status='ready', error=NULL, n_chunks=?, codes=? WHERE id=?",
                     (len(items), json.dumps(doc_codes), doc_id))
    retrieval.invalidate()


def delete_document(doc_id: str) -> bool:
    rows = db.query("SELECT path FROM documents WHERE id=?", (doc_id,))
    if not rows:
        return False
    with db.tx() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
        conn.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))  # chunks + codes cascade
    for path in config.FILES_DIR.glob(f"{doc_id}*"):
        path.unlink(missing_ok=True)
    retrieval.invalidate()
    return True


reindex_state = {"running": False, "done": 0, "total": 0, "error": None}


async def reindex() -> None:
    """Re-embed every chunk with the current embedding model and refresh ICS tags."""
    if reindex_state["running"]:
        return
    settings = config.load()
    rows = db.query("SELECT c.id, c.text, d.filename, d.codes FROM chunks c JOIN documents d ON d.id = c.doc_id")
    reindex_state.update(running=True, done=0, total=len(rows), error=None)
    embed_model = config.embed_id(settings)
    try:
        for i in range(0, len(rows), 32):
            batch = rows[i:i + 32]
            blobs = await embed_texts([f"{r['filename']}\n{r['text']}" for r in batch], settings)
            with db.tx() as conn:
                for row, blob in zip(batch, blobs):
                    conn.execute("UPDATE chunks SET embedding=?, embed_model=? WHERE id=?", (blob, embed_model, row["id"]))
                    conn.execute("DELETE FROM chunk_codes WHERE chunk_id=?", (row["id"],))
                    for code in dict.fromkeys(json.loads(row["codes"] or "[]") + ics.find_codes(row["text"])):
                        conn.execute("INSERT INTO chunk_codes(chunk_id, code) VALUES(?,?)", (row["id"], code))
            reindex_state["done"] += len(batch)
    except Exception as exc:
        reindex_state["error"] = str(exc)[:300]
    finally:
        reindex_state["running"] = False
        retrieval.invalidate()
