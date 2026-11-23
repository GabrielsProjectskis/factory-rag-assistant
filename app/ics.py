"""ICS code registry: one record per factory part, and detection of codes in text.

Detection is registry-first: any code that exists in the registry is recognised
exactly, whatever its format. The configurable regex (Settings -> ICS pattern) is a
fallback that also catches codes that are not registered yet.
"""
from __future__ import annotations

import csv
import io
import json
import re

from . import config, db

_ALIASES = {
    "code": {"code", "ics", "ics code", "ics_code", "ics-code", "icscode", "tag"},
    "name": {"name", "naam", "title", "titel", "part", "onderdeel"},
    "description": {"description", "beschrijving", "omschrijving", "function", "functie"},
    "area": {"area", "location", "locatie", "zone", "afdeling", "department"},
    "parent": {"parent", "parent code", "parent_code", "bovenliggend"},
}

_cache: dict = {"rx": None, "canon": {}, "dirty": True}


def invalidate() -> None:
    _cache["dirty"] = True


def _registry():
    if _cache["dirty"]:
        codes = [r["code"] for r in db.query("SELECT code FROM ics_codes")]
        _cache["canon"] = {c.upper(): c for c in codes}
        if codes:
            alt = "|".join(re.escape(c) for c in sorted(codes, key=len, reverse=True))
            _cache["rx"] = re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])", re.I)
        else:
            _cache["rx"] = None
        _cache["dirty"] = False
    return _cache["rx"], _cache["canon"]


def find_codes(text: str, pattern: str | None = None) -> list[str]:
    """ICS codes mentioned in `text`, registry spelling where known, first-seen order."""
    if not text:
        return []
    rx, canon = _registry()
    found: dict[str, None] = {}
    if rx:
        for m in rx.finditer(text):
            found.setdefault(canon[m.group(0).upper()])
    pattern = config.load()["ics_pattern"] if pattern is None else pattern
    if pattern:
        try:
            for m in re.finditer(pattern, text):
                code = m.group(0)
                found.setdefault(canon.get(code.upper(), code))
        except re.error:
            pass
    return list(found)


def upsert(code: str, name="", description="", area="", parent="", extra: dict | None = None) -> None:
    code = code.strip()
    if not code:
        return
    db.execute(
        "INSERT INTO ics_codes(code,name,description,area,parent,extra) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, description=excluded.description, "
        "area=excluded.area, parent=excluded.parent, extra=excluded.extra",
        (code, name.strip(), description.strip(), area.strip(), parent.strip(), json.dumps(extra or {})),
    )
    invalidate()


def import_csv(raw: bytes) -> int:
    """Import a registry export. Accepts , ; or tab separators and English/Dutch headers."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", "replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    mapping = {}
    for header in reader.fieldnames or []:
        key = (header or "").strip().lower()
        for field, names in _ALIASES.items():
            if key in names and field not in mapping.values():
                mapping[header] = field
    if "code" not in mapping.values():
        raise ValueError("No code column found. Expected a header such as 'code' or 'ics_code'.")
    count = 0
    for row in reader:
        rec = {"code": "", "name": "", "description": "", "area": "", "parent": ""}
        extra = {}
        for header, value in row.items():
            if header is None or value is None:
                continue
            value = value.strip()
            if header in mapping:
                rec[mapping[header]] = value
            elif value:
                extra[header.strip()] = value
        if rec["code"]:
            upsert(extra=extra, **rec)
            count += 1
    return count


def _row(r) -> dict:
    d = dict(r)
    d["extra"] = json.loads(d.get("extra") or "{}")
    return d


def get(code: str) -> dict | None:
    rows = db.query("SELECT * FROM ics_codes WHERE code = ? COLLATE NOCASE", (code,))
    return _row(rows[0]) if rows else None


def search(q: str = "", limit: int = 300) -> list[dict]:
    if q:
        like = f"%{q}%"
        rows = db.query(
            "SELECT * FROM ics_codes WHERE code LIKE ? OR name LIKE ? OR description LIKE ? OR area LIKE ? "
            "ORDER BY code LIMIT ?", (like, like, like, like, limit))
    else:
        rows = db.query("SELECT * FROM ics_codes ORDER BY code LIMIT ?", (limit,))
    return [_row(r) for r in rows]


def detail(code: str) -> dict:
    """Registry record plus everything in the library that mentions this code."""
    part = get(code) or {"code": code, "name": "", "description": "", "area": "", "parent": "",
                         "extra": {}, "unregistered": True}
    code = part["code"]
    part["documents"] = [dict(r) for r in db.query(
        "SELECT d.id, d.filename, d.kind, COUNT(*) AS mentions FROM chunk_codes cc "
        "JOIN chunks c ON c.id = cc.chunk_id JOIN documents d ON d.id = c.doc_id "
        "WHERE cc.code = ? GROUP BY d.id ORDER BY mentions DESC", (code,))]
    part["images"] = [dict(r) for r in db.query(
        "SELECT c.id AS chunk_id, c.text, d.filename FROM chunk_codes cc JOIN chunks c ON c.id = cc.chunk_id "
        "JOIN documents d ON d.id = c.doc_id WHERE cc.code = ? AND c.image_path IS NOT NULL LIMIT 12", (code,))]
    part["children"] = [dict(r) for r in db.query(
        "SELECT code, name FROM ics_codes WHERE parent = ? ORDER BY code", (code,))]
    return part
