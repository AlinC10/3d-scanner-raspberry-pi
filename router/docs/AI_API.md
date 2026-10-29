# AI Assistant API (`/api/ai`)

The AI router (`../ai.py`) exposes ScanAssist, the documentation assistant of the front-end. It answers from the
documents in `ai/ai-docs/` (Chroma database + Groq LLM). The assistant is **optional**: the scanner never needs it, and
a missing key or library never stops the server.

Interactive OpenAPI documentation: `http://127.0.0.1:8000/docs#/AI`

---

## Service state (`ai/service.py`)

| Field | Meaning |
|---|---|
| `on` | `false` at startup. Becomes `true` only after `GROQ_API_KEY` was found and the agent (Groq client, embedding model, Chroma database) loaded. |
| `loading` | `true` while the service loads in the background after the server started (the embedding model takes a few seconds). |
| `reason` | Why it is off (`null` when on): `GROQ_API_KEY is not set ...`, `The AI assistant could not be loaded: <error>`, or `The AI service is starting ...`. |

The lifespan of `main_prod.py` calls `service.start()`: the load runs in a daemon thread. Without `GROQ_API_KEY` the
agent is not even imported (no model is loaded).

* **Embedding model** (`paraphrase-multilingual-MiniLM-L12-v2`): downloaded once into `~/.cache/huggingface` on the
  first run (a few hundred MB), loaded into memory at every start.
* **Documentation database** (`ai/chroma_database/`): persists between runs. It is rebuilt only when it is empty or the
  documents in `ai/ai-docs/` changed since it was built (a fingerprint of their names and contents is saved as
  `ai/chroma_database/docs_fingerprint.txt`, see `ai/docs_index.py`). A rebuild removes every old chunk first.

---

## Endpoints

| Method | Path | Status codes | Description |
|---|---|---|---|
| `GET` | `/api/ai/status` | `200` | Service state (`AiStatusResponse`) |
| `POST` | `/api/ai/chat` | `200`, `503`, `500`, `422` | Answer a question (`ChatRequest` -> `ChatResponse`) |

### `GET /api/ai/status`
```json
{"on": false, "loading": false, "reason": "GROQ_API_KEY is not set (add it to .env to enable the AI assistant)."}
```
A front-end can poll it once at load (and again while `loading` is `true`) to enable or grey out the chat.

### `POST /api/ai/chat`
Request (`schemas/ai.py`, `ChatRequest`):
```json
{"question": "What LED intensity should I use?", "history": [{"role": "user", "text": "..."}, {"role": "assistant", "text": "..."}]}
```
Response `200` (`ChatResponse`): `{"response": "..."}`.

Errors:
* `503 Service Unavailable`: the assistant is off or still loading; `detail` is the `reason` of the status.
  (Before 2026-10-28 every failure was a `200` whose `response` held the error text; a front-end must now read `detail`
  on a non-2xx answer.)
* `500 Internal Server Error`: the agent raised while answering (`detail`: `AI assistant error: ...`). A failure of the
  Groq call itself is still returned by the agent as answer text (`Eroare API Groq: ...`).
* `422`: invalid body (`question` missing).

---

## AI documents (`/ai-docs/*`)

The documents the assistant answers from are served read-only at `/ai-docs/<file>` (for example
`/ai-docs/user_manual.md`). Not under `/docs`, which is the Swagger UI.
