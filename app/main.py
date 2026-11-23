"""HTTP API + static web UI. Run with:  python -m app"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chat, config, db, ics, ingest, providers

STATIC = Path(__file__).parent / "static"
_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    # Anything left 'processing' was interrupted by a restart.
    db.execute("UPDATE documents SET status='error', error='Interrupted - delete and upload again.' "
               "WHERE status='processing'")
    yield


app = FastAPI(title=config.APP_NAME, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
class _Static(StaticFiles):
    """Always revalidate, so an updated UI is picked up without a hard refresh."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _Static(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


# ---------- status & settings ----------

@app.get("/api/status")
def status():
    settings = config.load()
    roles = {}
    for name, role in settings["roles"].items():
        roles[name] = {**role, "local": config.provider_is_local(role["provider"], settings)}
    current = config.embed_id(settings)
    count = lambda sql, p=(): db.query(sql, p)[0][0]  # noqa: E731
    return {
        "app_name": config.APP_NAME,
        "private_mode": config.private_mode(),
        "all_local": all(r["local"] for r in roles.values()),
        "roles": roles,
        "documents": count("SELECT COUNT(*) FROM documents"),
        "parts": count("SELECT COUNT(*) FROM ics_codes"),
        "stale_chunks": count("SELECT COUNT(*) FROM chunks WHERE embed_model IS NOT ?", (current,)),
        "reindex": ingest.reindex_state,
    }


@app.get("/api/settings")
def get_settings():
    return config.public(config.load())


class SettingsPatch(BaseModel):
    roles: dict | None = None
    providers: dict | None = None
    ics_pattern: str | None = None
    top_k: int | None = None


@app.put("/api/settings")
def put_settings(patch: SettingsPatch):
    data: dict = {}
    current = config.load()
    if patch.roles:
        data["roles"] = {}
        for name, role in patch.roles.items():
            if name not in current["roles"]:
                continue
            provider, model = role.get("provider"), (role.get("model") or "").strip()
            if provider not in {p["id"] for p in config.PROVIDERS} or not model:
                raise HTTPException(400, f"Choose a provider and a model for '{name}'.")
            data["roles"][name] = {"provider": provider, "model": model}
    if patch.providers:
        data["providers"] = {}
        for name, cfg in patch.providers.items():
            if name not in current["providers"]:
                continue
            clean = {}
            if cfg.get("base_url"):
                clean["base_url"] = cfg["base_url"].strip()
            if cfg.get("api_key"):  # an empty field means "keep the saved key"
                clean["api_key"] = cfg["api_key"].strip()
            data["providers"][name] = clean
    if patch.ics_pattern is not None:
        try:
            re.compile(patch.ics_pattern)
        except re.error as exc:
            raise HTTPException(400, f"ICS pattern is not a valid regular expression: {exc}")
        data["ics_pattern"] = patch.ics_pattern
    if patch.top_k is not None:
        data["top_k"] = max(1, min(20, patch.top_k))
    merged = config.load()
    config._merge(merged, data)
    if config.private_mode():
        for name, role in merged["roles"].items():
            if not config.provider_is_local(role["provider"], merged):
                raise HTTPException(400, f"Private mode is on: the {name} model must use a local provider. "
                                         "Set PRIVATE_MODE=false on the server to allow cloud APIs.")
    return config.public(config.save(data))


@app.get("/api/models")
async def list_models(provider: str):
    """Models the provider actually has, for the Settings dropdowns. Empty on any failure."""
    try:
        return {"models": await providers.get(provider, config.load()).list_models()}
    except (providers.ProviderError, httpx.HTTPError, KeyError, ValueError):
        return {"models": []}


# ---------- library ----------

@app.get("/api/documents")
def documents():
    rows = db.query("SELECT id, filename, kind, size, status, error, n_chunks, codes, created_at "
                    "FROM documents ORDER BY created_at DESC")
    return {"documents": [{**dict(r), "codes": json.loads(r["codes"] or "[]")} for r in rows]}


@app.post("/api/documents")
async def upload(files: list[UploadFile] = File(...), codes: str = Form("")):
    extra = [c for c in re.split(r"[,;\s]+", codes) if c]
    created, rejected = [], []
    for file in files:
        data = await file.read()
        if len(data) > config.MAX_UPLOAD_BYTES:
            rejected.append({"filename": file.filename, "reason": "File is too large."})
            continue
        try:
            doc_id = ingest.create_document(file.filename or "upload", data)
        except ValueError as exc:
            rejected.append({"filename": file.filename, "reason": str(exc)})
            continue
        _spawn(ingest.process_document(doc_id, extra))
        created.append(doc_id)
    return {"created": created, "rejected": rejected}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str):
    if not ingest.delete_document(doc_id):
        raise HTTPException(404, "Document not found.")
    return {"ok": True}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str):
    rows = db.query("SELECT path, filename, mime FROM documents WHERE id=?", (doc_id,))
    if not rows or not Path(rows[0]["path"]).exists():
        raise HTTPException(404, "File not found.")
    return FileResponse(rows[0]["path"], media_type=rows[0]["mime"], filename=rows[0]["filename"],
                        content_disposition_type="inline")


@app.get("/api/chunks/{chunk_id}/image")
def chunk_image(chunk_id: int):
    rows = db.query("SELECT image_path FROM chunks WHERE id=?", (chunk_id,))
    if not rows or not rows[0]["image_path"] or not Path(rows[0]["image_path"]).exists():
        raise HTTPException(404, "Image not found.")
    return FileResponse(rows[0]["image_path"])


@app.post("/api/reindex")
async def reindex():
    _spawn(ingest.reindex())
    return {"ok": True}


# ---------- ICS registry ----------

@app.get("/api/ics")
def ics_list(q: str = ""):
    return {"parts": ics.search(q)}


@app.get("/api/ics/{code:path}")
def ics_detail(code: str):
    return ics.detail(code)


class Part(BaseModel):
    code: str
    name: str = ""
    description: str = ""
    area: str = ""
    parent: str = ""


@app.post("/api/ics")
def ics_upsert(part: Part):
    if not part.code.strip():
        raise HTTPException(400, "A code is required.")
    ics.upsert(**part.model_dump())
    return ics.get(part.code)


@app.post("/api/ics/import")
async def ics_import(file: UploadFile = File(...)):
    try:
        count = ics.import_csv(await file.read())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"imported": count}


@app.delete("/api/ics/{code:path}")
def ics_delete(code: str):
    db.execute("DELETE FROM ics_codes WHERE code=?", (code,))
    ics.invalidate()
    return {"ok": True}


# ---------- chat ----------

@app.get("/api/conversations")
def conversations():
    rows = db.query("SELECT id, title, created_at FROM conversations ORDER BY created_at DESC LIMIT 100")
    return {"conversations": [dict(r) for r in rows]}


@app.get("/api/conversations/{conv_id}")
def conversation(conv_id: str):
    rows = db.query("SELECT id, role, content, sources, image_path FROM messages WHERE conv_id=? ORDER BY id",
                    (conv_id,))
    return {"messages": [{
        "id": r["id"], "role": r["role"], "content": r["content"],
        "sources": json.loads(r["sources"]) if r["sources"] else [], "has_image": bool(r["image_path"]),
    } for r in rows]}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    for r in db.query("SELECT image_path FROM messages WHERE conv_id=? AND image_path IS NOT NULL", (conv_id,)):
        Path(r["image_path"]).unlink(missing_ok=True)
    db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    return {"ok": True}


@app.get("/api/messages/{message_id}/image")
def message_image(message_id: int):
    rows = db.query("SELECT image_path FROM messages WHERE id=?", (message_id,))
    if not rows or not rows[0]["image_path"] or not Path(rows[0]["image_path"]).exists():
        raise HTTPException(404, "Image not found.")
    return FileResponse(rows[0]["image_path"])


@app.post("/api/chat")
async def chat_endpoint(message: str = Form(""), conversation_id: str = Form(""),
                        image: UploadFile | None = File(None)):
    img = None
    if image is not None and image.filename:
        data = await image.read()
        if len(data) > config.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Image is too large.")
        mime = image.content_type or "image/jpeg"
        if not mime.startswith("image/"):
            raise HTTPException(400, "Only images can be attached to a question.")
        img = (data, mime, image.filename)
    if not message.strip() and not img:
        raise HTTPException(400, "Type a question or attach a photo.")
    return StreamingResponse(
        chat.stream_turn(message, conversation_id or None, img), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
