"""SQLite storage: documents, chunks (+ embeddings), full-text index, ICS registry, chats."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'document' | 'image'
    mime TEXT,
    size INTEGER,
    path TEXT NOT NULL,
    status TEXT NOT NULL,            -- 'processing' | 'ready' | 'error'
    error TEXT,
    n_chunks INTEGER DEFAULT 0,
    codes TEXT,                      -- JSON list: ICS codes that apply to the whole file
    created_at REAL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord INTEGER,
    text TEXT NOT NULL,
    page INTEGER,
    image_path TEXT,
    embed_model TEXT,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS chunk_codes (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    code TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_codes_code ON chunk_codes(code);
CREATE INDEX IF NOT EXISTS idx_chunk_codes_chunk ON chunk_codes(chunk_id);
CREATE TABLE IF NOT EXISTS ics_codes (
    code TEXT PRIMARY KEY,
    name TEXT,
    description TEXT,
    area TEXT,
    parent TEXT,
    extra TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sources TEXT,
    image_path TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    config.FILES_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params=()) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def execute(sql: str, params=()) -> int:
    with tx() as conn:
        return conn.execute(sql, params).lastrowid


def now() -> float:
    return time.time()
