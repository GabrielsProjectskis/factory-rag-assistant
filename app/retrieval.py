"""Hybrid retrieval: semantic (embeddings) + keyword (SQLite FTS5) + ICS-code matches,
fused with reciprocal rank fusion.

Keyword and code matching matter here: an ICS code is an exact identifier, and
embeddings alone are poor at telling 20-PMP-001 from 20-PMP-002.
"""
from __future__ import annotations

import re
from collections import defaultdict

import numpy as np

from . import config, db, ics, providers

_index: dict = {"key": None, "ids": None, "matrix": None}
_STOP = set("a an and are as at be by do does for from how i in is it my of on or the this to what when "
            "where which who why with de het een en van voor hoe wat is op te dat die in met".split())


def invalidate() -> None:
    _index["key"] = None


def _matrix(embed_model: str):
    if _index["key"] != embed_model:
        rows = db.query("SELECT id, embedding FROM chunks WHERE embed_model=? AND embedding IS NOT NULL", (embed_model,))
        if rows:
            ids = np.array([r["id"] for r in rows])
            matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        else:
            ids, matrix = np.array([], dtype=int), np.zeros((0, 0), dtype=np.float32)
        _index.update(key=embed_model, ids=ids, matrix=matrix)
    return _index["ids"], _index["matrix"]


def _fts_query(query: str, codes: list[str]) -> str:
    terms = []
    for code in codes:  # a code such as 20-PMP-001 is tokenised into 20 / PMP / 001: search it as a phrase
        tokens = re.findall(r"\w+", code)
        if tokens:
            terms.append('"' + " ".join(tokens) + '"')
    for tok in dict.fromkeys(re.findall(r"\w+", query.lower())):
        if len(tok) > 1 and tok not in _STOP:
            terms.append(f'"{tok}"')
    return " OR ".join(terms[:32])


async def search(query: str, settings: dict, top_k: int | None = None) -> tuple[list[dict], str | None]:
    """-> (chunks ranked best-first, optional warning for the user)."""
    top_k = top_k or settings["top_k"]
    codes = ics.find_codes(query, settings["ics_pattern"])
    warning = None
    sims: dict[int, float] = {}

    vector_rank: list[int] = []
    try:
        ids, matrix = _matrix(config.embed_id(settings))
        if len(ids):
            provider, model = providers.for_role("embedding", settings)
            qv = np.asarray((await provider.embed(model, [query]))[0], dtype=np.float32)
            qv /= (np.linalg.norm(qv) or 1.0)
            if qv.shape[0] != matrix.shape[1]:
                warning = "The embedding model changed. Re-index the library in Settings."
            else:
                scores = matrix @ qv
                for i in np.argsort(-scores)[:30]:
                    vector_rank.append(int(ids[i]))
                    sims[int(ids[i])] = float(scores[i])
    except providers.ProviderError as exc:
        warning = f"Semantic search unavailable, used keyword search only. {exc}"

    keyword_rank: list[int] = []
    fts = _fts_query(query, codes)
    if fts:
        keyword_rank = [r["rowid"] for r in db.query(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT 30", (fts,))]

    code_rank: list[int] = []
    if codes:
        marks = ",".join("?" * len(codes))
        code_rank = [r["chunk_id"] for r in db.query(
            f"SELECT DISTINCT chunk_id FROM chunk_codes WHERE code IN ({marks}) LIMIT 200", codes)]
        code_rank.sort(key=lambda cid: -sims.get(cid, -1.0))
        code_rank = code_rank[:30]

    fused: dict[int, float] = defaultdict(float)
    for weight, ranking in ((1.0, vector_rank), (1.0, keyword_rank), (1.5, code_rank)):
        for rank, chunk_id in enumerate(ranking):
            fused[chunk_id] += weight / (60 + rank + 1)
    best = sorted(fused, key=fused.get, reverse=True)[:top_k]
    if not best:
        return [], warning

    marks = ",".join("?" * len(best))
    rows = {r["id"]: r for r in db.query(
        f"SELECT c.id, c.text, c.page, c.image_path, c.doc_id, d.filename, d.kind FROM chunks c "
        f"JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({marks})", best)}
    code_rows = db.query(f"SELECT chunk_id, code FROM chunk_codes WHERE chunk_id IN ({marks})", best)
    chunk_codes: dict[int, list[str]] = defaultdict(list)
    for r in code_rows:
        chunk_codes[r["chunk_id"]].append(r["code"])

    results = []
    for chunk_id in best:
        r = rows.get(chunk_id)
        if r:
            results.append({
                "chunk_id": r["id"], "doc_id": r["doc_id"], "filename": r["filename"], "page": r["page"],
                "text": r["text"], "has_image": bool(r["image_path"]), "codes": chunk_codes[chunk_id],
                "score": round(fused[chunk_id], 5),
            })
    return results, warning
