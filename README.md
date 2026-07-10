# deepl-to-llm

> A DeepL-compatible translation API backed by an LLM — a drop-in replacement so any tool that speaks the DeepL HTTP API (Collabora Online, and friends) can translate with an LLM instead, **with zero client-side code changes.**

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](Dockerfile)

---

## ✨ What it does

This is a small FastAPI service that exposes a **DeepL-compatible `POST /translate` endpoint** backed by any **OpenAI-style chat-completions API** (Gemini via a proxy, GPT, local Llama, …). Because it speaks DeepL's wire format, applications wired to call DeepL can point at this bridge instead and get LLM-quality translation for free — no SDK swap, no request rewrite.

It was built for **Collabora Online's whole-document translation loop**, where the cost of a bad translation is high: Collabora walks every header, footer, paragraph, and table cell, and a single corrupted fragment cascades into runaway duplication or lost run formatting. So this project's headline feature isn't "it translates" — it's **"it never corrupts your document."**

### Highlights

- 🪄 **Drop-in DeepL replacement** — same auth, same body shape, same `?tag_handling=html`, same response envelope.
- 🧱 **Structure-preserving HTML translation** — the LLM never sees markup. Text is extracted, translated 1:1, and refilled into the original tags. Tags, attributes, and run boundaries all survive intact.
- 🛡️ **Fail-safe by design** — on any structural problem (count mismatch, parse error, translator failure) it returns the original text *untranslated*. A missed paragraph is a quality regression; a corrupted one is a data-loss incident.
- 🔁 **Optional real-DeepL passthrough with LLM fallback** — set `DEEPL_API_KEY` to forward to DeepL first and fall back to the LLM on quota/rate-limit errors. Great for A/B testing DeepL vs. the LLM on the same document *and* for keeping translation alive when DeepL runs dry.
- ⏱️ **Tuned for Collabora's 10-second client timeout** — every call budget is set to respond-or-failover before Collabora drops the connection.
- 🔐 **Flexible auth** — `Authorization: Bearer`, `Authorization: DeepL-Auth-Key`, a bare header, or a form `auth_key` field.

---

## 🧠 Why an LLM bridge instead of just calling the LLM?

The obvious approach — hand the raw HTML to the LLM with a "please preserve the tags" instruction — is unreliable. The model drops attributes, duplicates tags, and merges runs. In Collabora's whole-document loop, one corrupted paragraph snowballs into duplicated headers/footers across every page.

This bridge does it differently:

```
            ┌──────────────────────────────────────────────┐
   HTML ──▶ │  extract_text_nodes()                        │ ──▶ ["Hello", "world"]
            │  walk the tree, collect only the .text/.tail  │
            │  slots — never the markup itself              │
            └──────────────────────────────────────────────┘
                                  │
                                  ▼
            ┌──────────────────────────────────────────────┐
            │  translate_fn()  ──▶  LLM (JSON {items: [...]})   ──▶ DeepL-first fallback chain
            └──────────────────────────────────────────────┘
                                  │
                                  ▼
            ┌──────────────────────────────────────────────┐
  HTML ◀──  │  refill_text_nodes()                         │ ◀── ["你好", "世界"]
            │  put translations back in the SAME slots;     │
            │  raise on count mismatch (never misalign)     │
            └──────────────────────────────────────────────┘
```

The 1:1 count contract is enforced end-to-end with `response_format={"type": "json_object"}` and a strict system prompt. If the model breaks the contract, the bridge falls back to the untouched source rather than emit misaligned markup.

---

## 🚀 Quick start

### Docker (matches production)

```bash
docker build -t deepl-to-llm .
docker run -p 1188:1188 \
  -e LLM_API_URL=https://your-llm-endpoint/v1/chat/completions \
  -e LLM_API_KEY=sk-... \
  -e LLM_MODEL=gemini-2.5-flash \
  -e BRIDGE_TOKEN=your-secret-token \
  deepl-to-llm
```

A prebuilt image is published on every `Dockerfile` change to `main`:

```bash
docker pull ghcr.io/er-epr/deepl-to-llm:latest
```

### Local dev

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 1188 --reload
```

Then open the interactive Swagger UI at **http://localhost:1188/docs** to exercise the endpoint by hand.

---

## 🔧 Configuration

All config is via environment variables, read at import time (restart to change). **The in-code defaults are placeholders — set these in production.**

| Variable | Default | Description |
|---|---|---|
| `LLM_API_URL` | `https://example.com/v1/chat/completions` | Upstream OpenAI-style chat-completions URL. |
| `LLM_API_KEY` | *(empty)* | Bearer token sent to the LLM. Empty → no `Authorization` header. |
| `LLM_MODEL` | *(empty)* | Model name passed in the payload. |
| `BRIDGE_TOKEN` | *(empty)* | Token clients must present. **Empty disables auth** — anyone can hit `/translate`. Always set in production. |
| `LLM_TIMEOUT` | `8.5` | Seconds for the LLM call (kept under Collabora's hardcoded 10s). |
| `DEEPL_API_KEY` | *(empty)* | Optional. If set, forward to real DeepL first, fall back to the LLM on 429/456/network errors. A free key ending `:fx` auto-selects `api-free.deepl.com`; a Pro key selects `api.deepl.com`. |
| `DEEPL_API_URL` | *auto-detected* | Override the DeepL endpoint. |
| `DEEPL_TIMEOUT` | `9.0` | Seconds for the DeepL call. |
| `LOG_LEVEL` | `info` | `debug` logs every request/LLM-call/response; `info` logs backend choice + fallbacks; anything else silences. |

---

## 📡 API

### `POST /translate`

Accepts JSON or form-encoded bodies. `text` may be a string or an array (array → one translation per element, same order). Reads `target_lang` (a DeepL code, default `ZH`) and `tag_handling` from the **query string**.

**Plain text**

```bash
curl -X POST "http://localhost:1188/translate?target_lang=ZH" \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello, world!"}'
```

```json
{
  "translations": [
    {"detected_source_language": "EN", "text": "你好，世界！"}
  ]
}
```

**HTML (Collabora-style — `?tag_handling=html`)**

```bash
curl -X POST "http://localhost:1188/translate?target_lang=EN-US&tag_handling=html" \
  -H "Authorization: DeepL-Auth-Key your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"text": ["<p>Bonjour <b>le monde</b></p>", "Ceci est un test"]}'
```

The markup is parsed, text is extracted and translated 1:1, then refilled into the **original tags** — attributes, nesting, and run boundaries all preserved.

### `GET /health`

```json
{"status": "ok"}
```

---

## 🧪 Testing

**Unit + endpoint tests** (no network — the LLM is stubbed):

```bash
pip install pytest
python3 -m pytest test_bridge.py test_endpoint.py
```

**Live smoke test** against a real LLM (reads `LLM_API_URL` / `LLM_API_KEY` / `LLM_MODEL` from the environment — no keys baked in). Exercises Collabora-shaped payloads derived from `test.docx` headers/footers and verifies tag survival, real translation, preserved non-translatable tokens, no duplication, and the 1:1 array contract:

```bash
LLM_API_URL=... LLM_API_KEY=... LLM_MODEL=... python3 smoke_live.py
```

---

## 🏗️ Architecture

Two modules, each with a single responsibility:

### `bridge.py` — structure-preserving HTML translation (the core)

| Function | Purpose |
|---|---|
| `extract_text_nodes(html)` | Parse the fragment (inside a throwaway `<div>`, so multi-root fragments aren't wrapped in an extra `<span>`), walk in document order, collect translatable text from `.text`/`.tail` slots. Whitespace-only and `<script>`/`<style>`/`<code>`/`<pre>` content is skipped. |
| `refill_text_nodes(html, translations)` | Put translated strings back into the same slots. Raises `ValueError` on count mismatch — never silently misaligns. |
| `batch_texts(texts, max_items=500, max_chars=12000)` | A **safety net only**, not a context-chopper. Real paragraphs are small and must NEVER be split — cross-sentence context is what makes LLM translation accurate. The high thresholds only catch pathological payloads that could blow the time budget. |
| `translate_html(html, target_lang, translate_fn)` | Orchestrates extract → batch → translate → refill. On *any* structural problem it returns the original HTML untranslated. |
| `map_target_lang(code)` | Maps DeepL codes (`ZH`, `EN-US`, `ZH-HANS`, …) to human language names for the prompt; unknown codes fall back to the code itself. |

### `main.py` — endpoint + LLM call

1. **`verify_token`** — accepts the bridge token from `Authorization: Bearer <t>`, `Authorization: DeepL-Auth-Key <t>`, a bare `Authorization` header, or a form field `auth_key`.
2. **`llm_translate_items`** — sends text strings with `response_format={"type":"json_object"}` and a system prompt bound to `{"items":[...]}`, enforcing the 1:1 count contract. `temperature: 0`, plus a small non-zero Gemini thinking budget (the minimum needed to stop the model dropping short/low-context fragments to empty strings — which would otherwise cascade into header/footer duplication).
3. **`translate_items`** — the backend router: DeepL first (with `tag_handling=html` and `split_sentences=nonewlines`) when `DEEPL_API_KEY` is set, falling back to the LLM on any DeepL failure; LLM directly otherwise.
4. **`POST /translate`** — normalizes `text` (string or array), routes HTML through `bridge.translate_html` and plain text through `bridge.translate_plain`, and wraps results into DeepL's response envelope. `detected_source_language` is hardcoded `"EN"` (the bridge does no real language detection).

---

## 🔁 CI / release

[`.github/workflows/build-deepl-docker.yml`](.github/workflows/build-deepl-docker.yml) builds and pushes the Docker image to `ghcr.io/er-epr/deepl-to-llm:latest` on **push to `main` that touches `Dockerfile`**, plus `workflow_dispatch`. It uses GitHub Actions cache (`type=gha`). Changing `main.py` or `requirements.txt` alone will **not** trigger a build — also touch `Dockerfile`, or run the workflow manually.

---

## 📄 License

[Apache License 2.0](LICENSE).
