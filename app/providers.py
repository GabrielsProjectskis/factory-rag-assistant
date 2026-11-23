"""Model providers behind one small interface, so any role (chat / vision / embedding)
can be pointed at any backend:

    ollama  - local models (Gemma 4, EmbeddingGemma, ...)           -> stays on the network
    google  - Gemini API (Gemini + hosted Gemma)                    -> leaves the network
    openai  - any OpenAI-compatible server (LM Studio, vLLM, ...)   -> depends on its URL
    demo    - no model at all; lets the app run for UI demos and tests

A message is {"role": "user"|"assistant", "content": str, "images": [(bytes, mime), ...]}.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import zlib
from typing import AsyncIterator

import httpx

from . import config


class ProviderError(Exception):
    """A model backend failed in a way the user can act on."""


class TransientError(ProviderError):
    """Overloaded or rate limited: worth retrying after a short wait."""


def _client(read: float = 300.0) -> httpx.AsyncClient:
    """`read` is the longest silence allowed. Generous by default: a local model may need to load first."""
    return httpx.AsyncClient(timeout=httpx.Timeout(read, connect=5.0))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def _check(resp: httpx.Response, who: str, model: str) -> None:
    if resp.status_code < 400:
        return
    body = (await resp.aread()).decode("utf-8", "replace")
    try:
        err = json.loads(body).get("error", body)
        detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except ValueError:
        detail = body
    detail = detail.strip()[:300]
    if who == "Ollama" and resp.status_code == 404:
        raise ProviderError(f"Model '{model}' is not installed in Ollama. Run: ollama pull {model}")
    kind = TransientError if resp.status_code in (429, 500, 502, 503, 504) else ProviderError
    raise kind(f"{who} returned an error ({resp.status_code}): {detail}")


def _unreachable(who: str, url: str, exc: Exception) -> ProviderError:
    if isinstance(exc, httpx.ReadTimeout):
        return ProviderError(f"{who[0].upper()}{who[1:]} did not respond in time. The model may be overloaded; "
                             "try again, or choose another model in Settings.")
    return ProviderError(f"Can't reach {who} at {url}. Is it running? ({type(exc).__name__})")


class Ollama:
    label = "Ollama"

    def __init__(self, cfg: dict):
        self.base = cfg["base_url"].rstrip("/")

    async def chat_stream(self, model, system, messages, temperature=0.2) -> AsyncIterator[str]:
        msgs = [{"role": "system", "content": system}] if system else []
        for m in messages:
            item = {"role": m["role"], "content": m["content"]}
            if m.get("images"):
                item["images"] = [_b64(data) for data, _ in m["images"]]
            msgs.append(item)
        payload = {
            "model": model, "messages": msgs, "stream": True,
            # Thinking off: on a CPU the hidden reasoning costs a minute or more per answer, and the
            # answer is already grounded in retrieved sources.
            "think": False,
            "keep_alive": "30m",  # keep the model in memory between questions
            "options": {"temperature": temperature, "num_ctx": 8192},
        }
        try:
            async with _client() as client:
                for attempt in (1, 2):
                    async with client.stream("POST", f"{self.base}/api/chat", json=payload) as resp:
                        if resp.status_code == 400 and attempt == 1 and "think" in payload:
                            body = (await resp.aread()).decode("utf-8", "replace")
                            if "think" in body.lower():  # model has no thinking mode: ask again without the flag
                                del payload["think"]
                                continue
                            raise ProviderError(f"Ollama returned an error (400): {body.strip()[:300]}")
                        await _check(resp, "Ollama", model)
                        async for line in resp.aiter_lines():
                            if not line.strip():
                                continue
                            data = json.loads(line)
                            if data.get("error"):
                                raise ProviderError(f"Ollama: {data['error']}")
                            text = (data.get("message") or {}).get("content")
                            if text:
                                yield text
                        return
        except httpx.HTTPError as exc:
            raise _unreachable("Ollama", self.base, exc) from exc

    async def embed(self, model, texts):
        try:
            async with _client() as client:
                resp = await client.post(f"{self.base}/api/embed",
                                         json={"model": model, "input": texts, "keep_alive": "30m"})
                await _check(resp, "Ollama", model)
                return resp.json()["embeddings"]
        except httpx.HTTPError as exc:
            raise _unreachable("Ollama", self.base, exc) from exc

    async def list_models(self):
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{self.base}/api/tags")
            resp.raise_for_status()
            return sorted(m["name"] for m in resp.json().get("models", []))


class Google:
    label = "Google AI API"
    base = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, cfg: dict):
        self.key = cfg.get("api_key", "")

    def _headers(self):
        if not self.key:
            raise ProviderError("No Google API key set. Add one in Settings or as GOOGLE_API_KEY in .env.")
        return {"x-goog-api-key": self.key}

    async def chat_stream(self, model, system, messages, temperature=0.2) -> AsyncIterator[str]:
        contents = []
        for m in messages:
            parts = [{"text": m["content"]}] if m["content"] else []
            for data, mime in m.get("images") or []:
                parts.append({"inlineData": {"mimeType": mime, "data": _b64(data)}})
            contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": parts})
        payload = {"contents": contents, "generationConfig": {"temperature": temperature}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{self.base}/models/{model}:streamGenerateContent?alt=sse"
        try:
            async with _client(45.0) as client, client.stream("POST", url, json=payload, headers=self._headers()) as resp:
                await _check(resp, "Google AI API", model)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = json.loads(line[5:])
                    if data.get("error"):  # the API can fail part-way through a stream
                        err = data["error"]
                        kind = TransientError if err.get("code") in (429, 500, 502, 503, 504) else ProviderError
                        raise kind(f"Google AI API returned an error ({err.get('code')}): {err.get('message', '')[:300]}")
                    for cand in data.get("candidates", []):
                        for part in (cand.get("content") or {}).get("parts", []):
                            if part.get("text") and not part.get("thought"):
                                yield part["text"]
        except httpx.HTTPError as exc:
            raise _unreachable("the Google AI API", self.base, exc) from exc

    async def embed(self, model, texts):
        out = []
        try:
            async with _client(45.0) as client:
                for i in range(0, len(texts), 100):
                    reqs = [
                        {"model": f"models/{model}", "content": {"parts": [{"text": t}]}}
                        for t in texts[i:i + 100]
                    ]
                    resp = await client.post(
                        f"{self.base}/models/{model}:batchEmbedContents",
                        json={"requests": reqs}, headers=self._headers(),
                    )
                    await _check(resp, "Google AI API", model)
                    out.extend(e["values"] for e in resp.json()["embeddings"])
        except httpx.HTTPError as exc:
            raise _unreachable("the Google AI API", self.base, exc) from exc
        return out

    async def list_models(self):
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"{self.base}/models?pageSize=1000", headers=self._headers())
            resp.raise_for_status()
            return sorted(m["name"].removeprefix("models/") for m in resp.json().get("models", []))


class OpenAICompat:
    label = "OpenAI-compatible server"

    def __init__(self, cfg: dict):
        self.base = cfg["base_url"].rstrip("/")
        self.key = cfg.get("api_key", "")

    def _headers(self):
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    async def chat_stream(self, model, system, messages, temperature=0.2) -> AsyncIterator[str]:
        msgs = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m.get("images"):
                content = [{"type": "text", "text": m["content"]}]
                for data, mime in m["images"]:
                    content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{_b64(data)}"}})
                msgs.append({"role": m["role"], "content": content})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        payload = {"model": model, "messages": msgs, "stream": True, "temperature": temperature}
        try:
            async with _client() as client, client.stream(
                "POST", f"{self.base}/chat/completions", json=payload, headers=self._headers()
            ) as resp:
                await _check(resp, self.label, model)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    choices = json.loads(body).get("choices") or []
                    text = (choices[0].get("delta") or {}).get("content") if choices else None
                    if text:
                        yield text
        except httpx.HTTPError as exc:
            raise _unreachable(self.label, self.base, exc) from exc

    async def embed(self, model, texts):
        try:
            async with _client() as client:
                resp = await client.post(
                    f"{self.base}/embeddings", json={"model": model, "input": texts}, headers=self._headers()
                )
                await _check(resp, self.label, model)
                data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
                return [d["embedding"] for d in data]
        except httpx.HTTPError as exc:
            raise _unreachable(self.label, self.base, exc) from exc

    async def list_models(self):
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{self.base}/models", headers=self._headers())
            resp.raise_for_status()
            return sorted(m["id"] for m in resp.json().get("data", []))


class Demo:
    """No model: hashed bag-of-words embeddings and an extractive 'answer'."""
    label = "Demo"
    DIM = 256

    def __init__(self, cfg: dict | None = None):
        pass

    async def chat_stream(self, model, system, messages, temperature=0.2) -> AsyncIterator[str]:
        last = messages[-1]
        if last.get("images"):
            yield "An uploaded image (demo mode cannot look at images - connect a vision model in Settings)."
            return
        blocks = re.findall(r'<source id="(\d+)"[^>]*>\n(.*?)\n</source>', last["content"], re.S)
        if not blocks:
            text = "**Demo mode** - no language model is connected and nothing relevant was found in the library."
        else:
            lines = ["**Demo mode** - no language model is connected, so here are the most relevant passages:", ""]
            for n, body in blocks[:4]:
                snippet = " ".join(body.split())[:240]
                lines.append(f"- {snippet}... [{n}]")
            text = "\n".join(lines)
        for piece in re.findall(r"\S+\s*", text):
            yield piece
            await asyncio.sleep(0.008)

    async def embed(self, model, texts):
        out = []
        for text in texts:
            vec = [0.0] * self.DIM
            for tok in re.findall(r"[a-z0-9]+", text.lower()):
                h = zlib.crc32(tok.encode())
                vec[h % self.DIM] += 1.0 if (h >> 16) & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    async def list_models(self):
        return ["demo"]


class _Retrying:
    """Wraps a provider: retries overloaded / rate-limited calls with a growing pause.
    A chat stream is only retried if nothing has been sent to the user yet."""
    DELAYS = (2, 5, 12)

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def chat_stream(self, model, system, messages, temperature=0.2) -> AsyncIterator[str]:
        for delay in (*self.DELAYS, None):
            started = False
            try:
                async for token in self._inner.chat_stream(model, system, messages, temperature):
                    started = True
                    yield token
                return
            except TransientError:
                if started or delay is None:
                    raise
                await asyncio.sleep(delay)

    async def embed(self, model, texts):
        for delay in (*self.DELAYS, None):
            try:
                return await self._inner.embed(model, texts)
            except TransientError:
                if delay is None:
                    raise
                await asyncio.sleep(delay)


_CLASSES = {"ollama": Ollama, "google": Google, "openai": OpenAICompat, "demo": Demo}


def get(provider: str, settings: dict):
    if provider not in _CLASSES:
        raise ProviderError(f"Unknown provider '{provider}'.")
    if config.private_mode() and not config.provider_is_local(provider, settings):
        raise ProviderError(
            f"Private mode is on, so the {_CLASSES[provider].label} is blocked: it would send factory "
            "data outside the network. Use a local provider, or set PRIVATE_MODE=false on the server."
        )
    return _Retrying(_CLASSES[provider](settings["providers"].get(provider, {})))


def for_role(role: str, settings: dict):
    """-> (provider instance, model name) for 'chat', 'vision' or 'embedding'."""
    cfg = settings["roles"][role]
    return get(cfg["provider"], settings), cfg["model"]


async def complete(provider, model, system, messages, temperature=0.1) -> str:
    return "".join([tok async for tok in provider.chat_stream(model, system, messages, temperature)]).strip()
