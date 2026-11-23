# Factory Assistant

A private, self-hosted RAG assistant for a factory. Operators and technicians ask what a part
is for, how to operate it and how to maintain it. Answers are written from the company's own
manuals, drawings and photos, and every claim links back to its source.

Every part of the factory has an **ICS code**. The assistant treats those codes as first-class:
documents and images are tagged with the codes they mention, and a question about a code pulls
up that part's record and documentation first.

## How it works

```
 Library upload                         Question (+ optional photo)
      |                                          |
  parse PDF / Word / text / image         vision model describes the photo
      |                                          |
  vision model describes images           detect ICS codes -> part records
      |                                          |
  chunk -> tag ICS codes -> embed         hybrid search: embeddings + keywords + ICS-code matches
      |                                          |
  SQLite (text, vectors, FTS5, codes) --> small chat model answers from the sources, with citations
```

- **Two models, two jobs.** A vision model reads images (once at upload, and when a user attaches
  a photo). A small chat model writes the answer. A third, tiny embedding model powers search.
- **Any model.** Each of the three roles can use Ollama (local), the Google AI API, or any
  OpenAI-compatible server. Change them in Settings; no code changes.
- **Hybrid retrieval.** Embeddings find passages by meaning, SQLite FTS5 finds exact words, and
  ICS-code matches are boosted. The three rankings are merged with reciprocal rank fusion. Exact
  matching matters because embeddings alone cannot tell `20-PMP-001` from `20-PMP-002`.
- **One process, one file.** FastAPI serves the API and the web UI. Data lives in one SQLite
  database plus an uploads folder, so backup is copying `data/`.

## Privacy

`PRIVATE_MODE=true` is the default. In private mode the server refuses to call any model that is
not on this machine or the internal network, and cloud providers are greyed out in Settings. It
can only be changed in the server environment, not from the browser. The UI has no CDN, font or
analytics requests.

Using the Google AI API means questions, document text and photos are sent to Google. That is a
deliberate choice: set `PRIVATE_MODE=false`, and the sidebar badge changes from
"Private - runs locally" to "Using a cloud API".

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp
python -m app                   # http://localhost:8000
```

### Option A - local and private: Gemma 4 with Ollama (recommended)

Install [Ollama](https://ollama.com), then:

```bash
ollama pull gemma4:e4b          # small chat model
ollama pull gemma4:12b          # vision model (gemma4:e4b also works on a small machine)
ollama pull embeddinggemma      # embeddings
```

These are the defaults in `.env.example`. Gemma 4 is multimodal, so one model can fill both the
chat and vision roles if memory is tight (set `VISION_MODEL=gemma4:e4b` and skip the 12b download).

**Hardware.** Measured on a laptop without a dedicated GPU (Ryzen 5 7535HS, 27 GB RAM, CPU only)
with `gemma4:e4b` for chat and vision: the first words of an answer appear after about 30-45
seconds, and describing an uploaded image takes one to two minutes. That is usable for a demo, not
for daily work. For production use a server with a GPU; the larger vision model then becomes
practical as well. Gemma's hidden "thinking" step is switched off for local models because it more
than doubled the wait on CPU without changing the answers in testing.

### Option B - Google AI API (not used in the on-premise product)

In `.env` set `PRIVATE_MODE=false` and `GOOGLE_API_KEY=...`, then pick models in Settings
(for example chat `gemma-4-26b-a4b-it`, vision `gemini-4-hydrogen`, embeddings
`gemini-embedding-2`). The model fields list whatever your key has access to, so newer or larger
Gemini models can be selected there.

### Option C - no model at all (demo)

Choose "Demo (no model)" for all three roles in Settings. Search, ICS tagging and the whole UI
work; answers are the best-matching passages instead of written text. Useful for UI demos and it
is what the tests run against.

### Docker (assistant + Ollama on one internal server)

```bash
docker compose up -d --build
docker compose exec ollama ollama pull gemma4:e4b
docker compose exec ollama ollama pull gemma4:12b
docker compose exec ollama ollama pull embeddinggemma
docker compose exec assistant python -m scripts.seed_demo   # optional: sample factory
```

- The models are downloaded once into the `ollama-models` volume; documents, the index and chats
  live in the `assistant-data` volume and survive restarts and rebuilds.
- `CHAT_MODEL`, `VISION_MODEL` and `EMBEDDING_MODEL` can be overridden from the shell or a `.env`
  file next to `docker-compose.yml`.
- The app is published on `127.0.0.1:8000` only, because it has no login. To serve the internal
  network, put it behind the company reverse proxy / SSO, or set `BIND_ADDRESS=0.0.0.0` on a
  trusted network.
- Give Docker at least 12 GB of memory: `gemma4:e4b` used about 9.5 GB while answering.
- Private mode stops the app from calling cloud APIs. The containers themselves still have
  internet access (needed for `ollama pull`); block outbound traffic at the firewall afterwards
  if the company wants a network-level guarantee as well.

## Stop it

Stopping never deletes anything: documents, parts, chats and the downloaded models stay on disk.

**Local run (`python -m app`)**

1. Press `Ctrl+C` in the terminal where the app is running.
2. Stop Ollama, which otherwise keeps the model in memory (several GB) for 30 minutes after the
   last question: right-click the Ollama icon in the system tray and choose **Quit Ollama**, or:

```powershell
ollama stop gemma4:e4b                        # unload the model, keep Ollama running
Get-Process ollama* | Stop-Process -Force     # Windows: stop Ollama completely
```

On macOS/Linux use `pkill ollama` (or `systemctl stop ollama` if it runs as a service). Ollama
starts again at login on Windows; turn that off under Settings -> Apps -> Startup if unwanted.

If the terminal was closed and the app is still holding port 8000:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

**Docker**

```bash
docker compose stop        # stop both containers, keep everything
docker compose down        # also remove the containers; data and models are kept in the volumes
docker compose down -v     # remove the volumes too: deletes documents, chats and the models (~7 GB)
```

Start again with `python -m app` (after starting Ollama) or `docker compose up -d`.

## Loading data

Sample data (a small fictional factory) so the app can be tried before the real data arrives:

```bash
python -m scripts.seed_demo          # with the configured models
python -m scripts.seed_demo --demo   # without any model
```

When the real data is available:

1. **Parts -> Import CSV.** One row per ICS code. Columns are matched by header name (`code`,
   `name`, `description`, `area`, `parent`; Dutch headers such as `naam`, `omschrijving`,
   `locatie` work too). Comma, semicolon and tab separated files are accepted. Extra columns are
   kept and shown on the part.
2. **Settings -> ICS code pattern.** The default `\b\d{2}-[A-Z]{2,4}-\d{3}\b` is a placeholder
   that matches the sample data. Replace it with a regular expression for the real code format.
   Codes in the registry are always recognised regardless of the pattern.
3. **Library.** Drop in manuals, drawings and photos (PDF, Word, text, Markdown, CSV, PNG, JPEG,
   WebP). Codes in the file name or title are applied to the whole file; codes can also be typed
   in at upload.
4. Delete the sample files in Library and the sample parts, or start clean by removing `data/`.

## Tests

```bash
pytest
```

## Project layout

```
app/
  config.py      settings, private-mode rules
  providers.py   Ollama / Google / OpenAI-compatible / demo behind one interface
  ingest.py      parsing, chunking, image description, embedding
  ics.py         ICS registry and code detection
  retrieval.py   hybrid search with rank fusion
  chat.py        one chat turn, streamed
  main.py        HTTP API
  static/        web UI (plain HTML, CSS, JavaScript)
sample_data/     fictional factory
scripts/         seed_demo.py
tests/
```

## Known limits and next steps

- **No login.** The app binds to `127.0.0.1` by default. Before serving it to the network
  (`HOST=0.0.0.0`), put it behind the company's reverse proxy / single sign-on.
- **Scanned PDFs** without a text layer are not read. Next step: render such pages and pass them
  to the vision model.
- **Vector search is brute force** in NumPy. Fine for tens of thousands of passages; beyond that,
  swap `retrieval.py` for sqlite-vec or a vector database.
- **No evaluation set yet.** Once real documents exist, a small set of question / expected-source
  pairs would measure retrieval quality and guide chunk size and model choice.
- The assistant is a documentation aid. It does not replace site safety procedures.
