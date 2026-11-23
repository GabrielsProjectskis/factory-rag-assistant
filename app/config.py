"""Settings: defaults <- environment (.env) <- data/settings.json (saved from the UI).

PRIVATE_MODE is environment-only on purpose: whoever hosts the app decides whether
data may ever leave the network, and nobody can switch that off from the browser.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "assistant.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
APP_NAME = os.getenv("APP_NAME", "Factory Assistant")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024

PROVIDERS = [
    {"id": "ollama", "label": "Ollama (local)"},
    {"id": "google", "label": "Google AI API"},
    {"id": "openai", "label": "OpenAI-compatible server"},
    {"id": "demo", "label": "Demo (no model)"},
]

# Shown as suggestions in the UI; any model name can be typed in.
SUGGESTED = {
    "ollama": {
        "chat": ["gemma4:e4b", "gemma4:e2b", "gemma4:12b"],
        "vision": ["gemma4:12b", "gemma4:26b", "gemma4:e4b"],
        "embedding": ["embeddinggemma", "bge-m3", "nomic-embed-text"],
    },
    "google": {
        "chat": ["gemma-4-26b-a4b-it", "gemma-4-31b-it", "gemini-3.8-flash"],
        "vision": ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-4-carbon", "gemini-4-argon", "gemma-4-31b-it"],
        "embedding": ["gemini-embedding-2", "gemini-embedding-001"],
    },
    "openai": {"chat": [], "vision": [], "embedding": []},
    "demo": {"chat": ["demo"], "vision": ["demo"], "embedding": ["demo"]},
}


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def private_mode() -> bool:
    return _env_bool("PRIVATE_MODE", True)


def _defaults() -> dict:
    def role(name: str, model: str) -> dict:
        provider = os.getenv(f"{name.upper()}_PROVIDER", "ollama")
        fallback = model if provider == "ollama" else (SUGGESTED.get(provider, {}).get(name) or ["demo"])[0]
        return {"provider": provider, "model": os.getenv(f"{name.upper()}_MODEL", fallback)}

    return {
        "roles": {
            "chat": role("chat", "gemma4:e4b"),
            "vision": role("vision", "gemma4:12b"),
            "embedding": role("embedding", "embeddinggemma"),
        },
        "providers": {
            "ollama": {"base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")},
            "google": {"api_key": ""},
            "openai": {"base_url": os.getenv("OPENAI_BASE_URL", "http://localhost:1234/v1"), "api_key": ""},
        },
        # Placeholder format (e.g. 20-PMP-001) until the real ICS code format is known.
        "ics_pattern": os.getenv("ICS_PATTERN", r"\b\d{2}-[A-Z]{2,4}-\d{3}\b"),
        "top_k": 6,
    }


def _merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load() -> dict:
    settings = _defaults()
    if SETTINGS_PATH.exists():
        try:
            _merge(settings, json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    # Keys saved in the UI win; otherwise fall back to the environment.
    if not settings["providers"]["google"].get("api_key"):
        settings["providers"]["google"]["api_key"] = os.getenv("GOOGLE_API_KEY", "")
    if not settings["providers"]["openai"].get("api_key"):
        settings["providers"]["openai"]["api_key"] = os.getenv("OPENAI_API_KEY", "")
    return settings


def save(patch: dict) -> dict:
    stored = {}
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
    _merge(stored, copy.deepcopy(patch))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    return load()


def is_local_url(url: str) -> bool:
    """True when the host is this machine or a private/internal network address."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or "." not in host and ":" not in host:
        return True  # localhost, or a bare internal name such as a docker service
    if host.endswith((".local", ".internal", ".lan", ".localhost", ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def provider_is_local(provider: str, settings: dict) -> bool:
    if provider == "demo":
        return True
    if provider == "google":
        return False
    return is_local_url(settings["providers"].get(provider, {}).get("base_url", ""))


def embed_id(settings: dict) -> str:
    role = settings["roles"]["embedding"]
    return f"{role['provider']}:{role['model']}"


def public(settings: dict) -> dict:
    """Settings as sent to the browser: API keys are never returned."""
    out = copy.deepcopy(settings)
    for name in ("google", "openai"):
        key = out["providers"][name].pop("api_key", "")
        out["providers"][name]["api_key_set"] = bool(key)
    out["private_mode"] = private_mode()
    out["suggested"] = SUGGESTED
    out["provider_list"] = [
        {**p, "local": provider_is_local(p["id"], settings)} for p in PROVIDERS
    ]
    return out
