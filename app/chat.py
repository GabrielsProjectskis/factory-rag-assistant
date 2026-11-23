"""One chat turn: (optional photo -> vision model) -> retrieve -> small chat model, streamed as SSE."""
from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

from . import config, db, ics, providers, retrieval

SYSTEM_PROMPT = """You are {name}, the internal assistant for this factory. You help operators and \
technicians understand what each part of the factory is for, how to use it, and how to maintain it. \
Parts are identified by ICS codes.

Rules:
- Answer only from the sources and part records provided with the question. If they do not contain \
the answer, say so plainly and suggest what document or ICS code would help. Never invent values, \
settings, intervals, part numbers or procedures.
- Cite the sources you use with their number in square brackets, like [1] or [2][3].
- Mention the ICS code of the part you are talking about.
- For maintenance or repair steps, give them as a numbered list and repeat any safety precautions \
the sources state (isolation, lock-out, PPE). If the sources give none, tell the user to follow \
site safety procedures before working on equipment.
- Be concise and practical. Reply in the language the user writes in."""

PHOTO_PROMPT = (
    "A factory technician sent this photo with a question. Describe what the photo shows so the right "
    "documentation can be found: the type of equipment or part, visible damage or abnormal condition, "
    "and transcribe exactly any labels, tag numbers, codes or display readings. Be factual; under 120 words."
)


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _context(results: list[dict], parts: list[dict], image_desc: str) -> str:
    blocks = []
    for n, r in enumerate(results, 1):
        page = f' page="{r["page"]}"' if r["page"] else ""
        blocks.append(f'<source id="{n}" file="{r["filename"]}"{page}>\n{r["text"]}\n</source>')
    out = "\n\n".join(blocks) if blocks else "(No matching sources were found in the library.)"
    if parts:
        lines = []
        for p in parts:
            bits = [f"ICS code {p['code']}", p.get("name") or "", p.get("description") or ""]
            if p.get("area"):
                bits.append(f"Area: {p['area']}")
            if p.get("parent"):
                bits.append(f"Part of: {p['parent']}")
            lines.append(" - ".join(b for b in bits if b))
        out += "\n\nPart records from the ICS registry:\n" + "\n".join(lines)
    if image_desc:
        out += f"\n\nThe user attached a photo. What it shows: {image_desc}"
    return out


async def stream_turn(message: str, conv_id: str | None, image: tuple[bytes, str, str] | None) -> AsyncIterator[str]:
    settings = config.load()
    message = message.strip()
    title = (message or "Photo question")[:60]
    if conv_id and db.query("SELECT 1 FROM conversations WHERE id=?", (conv_id,)):
        history = db.query(
            "SELECT role, content FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT 6", (conv_id,))[::-1]
    else:
        conv_id = uuid.uuid4().hex[:12]
        db.execute("INSERT INTO conversations(id, title, created_at) VALUES(?,?,?)", (conv_id, title, db.now()))
        history = []
    yield _event("meta", {"conversation_id": conv_id})

    image_path = None
    if image:
        ext = {"image/png": ".png", "image/webp": ".webp"}.get(image[1], ".jpg")
        path = config.FILES_DIR / f"chat_{uuid.uuid4().hex[:12]}{ext}"
        path.write_bytes(image[0])
        image_path = str(path)
    db.execute("INSERT INTO messages(conv_id, role, content, image_path, created_at) VALUES(?,?,?,?,?)",
               (conv_id, "user", message, image_path, db.now()))

    try:
        image_desc = ""
        if image:
            yield _event("status", {"text": "Looking at your photo"})
            provider, model = providers.for_role("vision", settings)
            image_desc = await providers.complete(
                provider, model, PHOTO_PROMPT,
                [{"role": "user", "content": message or "What is this?", "images": [(image[0], image[1])]}])

        yield _event("status", {"text": "Searching the library"})
        query = f"{message} {image_desc}".strip()
        previous = [h["content"] for h in history if h["role"] == "user"]
        if previous and len(message.split()) < 8:  # short follow-up ("and how often?"): keep the topic
            query = f"{query} {previous[-1]}"
        results, warning = await retrieval.search(query[:2000], settings)

        codes = ics.find_codes(query, settings["ics_pattern"])
        if not codes and results:  # no code in the question: use the part the best source is about
            codes = results[0]["codes"][:3]
        parts = [p for p in (ics.get(c) for c in codes[:6]) if p]

        sources = [{
            "n": n, "doc_id": r["doc_id"], "chunk_id": r["chunk_id"], "filename": r["filename"],
            "page": r["page"], "snippet": r["text"][:320], "has_image": r["has_image"], "codes": r["codes"],
        } for n, r in enumerate(results, 1)]
        yield _event("sources", {"sources": sources, "warning": warning,
                                 "parts": [{"code": p["code"], "name": p["name"]} for p in parts]})

        yield _event("status", {"text": "Writing"})
        provider, model = providers.for_role("chat", settings)
        msgs = [{"role": h["role"], "content": h["content"]} for h in history if h["content"]]
        question = message or "What is shown in the photo, and what should I know about it?"
        msgs.append({"role": "user", "content": f"{_context(results, parts, image_desc)}\n\nQuestion: {question}"})
        answer = ""
        async for token in provider.chat_stream(model, SYSTEM_PROMPT.format(name=config.APP_NAME), msgs):
            answer += token
            yield _event("token", {"text": token})
        if not answer.strip():
            raise providers.ProviderError("The model returned an empty answer. Ask again, or choose another "
                                          "chat model in Settings.")

        db.execute("INSERT INTO messages(conv_id, role, content, sources, created_at) VALUES(?,?,?,?,?)",
                   (conv_id, "assistant", answer, json.dumps(sources), db.now()))
        yield _event("done", {})
    except providers.ProviderError as exc:
        yield _event("error", {"message": str(exc)})
    except Exception as exc:  # keep the stream well-formed whatever happens
        yield _event("error", {"message": f"Something went wrong: {type(exc).__name__}: {exc}"})
