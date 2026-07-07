"""DeepL-compatible translation bridge backed by an LLM.

Exposes POST /translate that mimics the DeepL text API closely enough for
Collabora Online's whole-document translation:
  - auth via Authorization: Bearer/DeepL-Auth-Key, or form `auth_key`
  - JSON or form-encoded body
  - `text` may be a string or array (array -> one translation per element)
  - ?tag_handling=html -> markup is preserved structurally (see bridge.py)
  - response: {"translations": [{"detected_source_language","text"}, ...]}

The HTML path does NOT hand markup to the LLM. It parses the fragment,
extracts the translatable text, sends only the text with a JSON-structured
output contract, validates the 1:1 response, and refills the original
markup. This prevents the tag/attribute corruption that caused runaway
header/footer duplication (Bug 1) and lost run formatting (Bug 2).
"""
import json
import os
import sys
import time
from typing import List, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request

import bridge

app = FastAPI()


def _log(level: str, msg: str) -> None:
    """Log to stderr if LOG_LEVEL permits. Levels: DEBUG < INFO < (off).

    LOG_LEVEL=debug -> everything; LOG_LEVEL=info (or unset) -> info+;
    any other value (e.g. "off") -> nothing. Goes to stderr so docker logs
    captures it.
    """
    cfg = os.getenv("LOG_LEVEL", "info").lower()
    levels = {"debug": 0, "info": 1}
    if cfg not in levels:
        return  # off
    if levels.get(level, 99) < levels[cfg]:
        return
    print(msg, file=sys.stderr, flush=True)


def _trunc(s: str, n: int = 800) -> str:
    """Truncate a string for logging, showing head + tail + length."""
    if s is None:
        return "<None>"
    if len(s) <= n:
        return s
    return f"{s[:n//2]}…[{len(s)} chars]…{s[-n//4:]}"

# Config is read at import time (module-level constants -> restart to change).
# All of these MUST be set via environment variables (e.g. in docker-compose);
# the defaults below are placeholders only and will not work against a real LLM.
LLM_API_URL = os.getenv(
    "LLM_API_URL", "https://example.com/v1/chat/completions"
)
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
# BRIDGE_TOKEN gates /translate. If left empty, auth is DISABLED (anyone can
# use the endpoint) — always set it in production.
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")

# Collabora's libcurl client uses a hardcoded 10s total timeout
# (CURLOPT_TIMEOUT=10L in translate.cxx). Stay under it so the bridge
# responds — or fails over to untranslated — before Collabora drops the
# connection and leaves a paragraph half-translated. 8.5s leaves margin for
# network jitter between the bridge and Collabora.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "8.5"))

TRANSLATOR_SYSTEM_PROMPT = (
    "You are a professional translator. Translate each string in the JSON "
    'object {{"items": [...]}} into {target}. Output ONLY a JSON object with '
    'the key "items" containing the translations, in the SAME ORDER and the '
    "SAME COUNT as the input. Do not add explanations, do not merge or split "
    "items, do not wrap in markdown. Preserve numbers, codes, and punctuation."
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def verify_token(request: Request, authorization: Optional[str] = Header(None)):
    """Accept the bridge token from Bearer / DeepL-Auth-Key header or form auth_key."""
    if not BRIDGE_TOKEN:
        return

    token_to_check = None
    if authorization:
        if authorization.startswith("Bearer "):
            token_to_check = authorization[len("Bearer "):]
        elif authorization.startswith("DeepL-Auth-Key "):
            token_to_check = authorization[len("DeepL-Auth-Key "):]
        else:
            token_to_check = authorization

    if not token_to_check:
        # DeepL allows auth_key in the form body; reading it consumes the body,
        # so only do it when the header was absent.
        form_data = await request.form()
        token_to_check = form_data.get("auth_key")

    if token_to_check != BRIDGE_TOKEN:
        print(f"Auth Failed: Received {token_to_check}")
        raise HTTPException(status_code=403, detail="Invalid API Key")


# ---------------------------------------------------------------------------
# LLM call with JSON-structured output
# ---------------------------------------------------------------------------

def llm_translate_items(texts: List[str], target_lang_name: str) -> List[str]:
    """Send text strings to the LLM and get back translations, 1:1.

    Uses response_format=json_object so the model is forced to emit valid
    JSON. The system prompt binds it to a fixed schema {"items": [...]}.
    Returns exactly len(texts) strings, or raises (caller falls back).
    """
    if not texts:
        return []

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": TRANSLATOR_SYSTEM_PROMPT.format(target=target_lang_name),
            },
            {
                "role": "user",
                "content": json.dumps({"items": texts}, ensure_ascii=False),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        # Gemini "thinking" budget. Translation needs a SMALL but NON-ZERO
        # budget: with thinkingBudget=0 the model drops short/low-context
        # fragments (e.g. a lone " 页" with a leading space) to empty strings,
        # producing empty <span></span> — which in Collabora's whole-document
        # loop cascades into runaway header/footer duplication. 512 is the
        # minimum effective value (verified: 0/128 drop fragments, 512+ do
        # not, 3/3 reproducible). Higher values add latency without quality
        # gain. Real paragraphs finish in 3-4s at 512 — under Collabora's 10s.
        # Passed through cliproxy's generationConfig passthrough to Gemini.
        "generationConfig": {"thinkingConfig": {"thinkingBudget": 512}},
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}

    _log("debug", f"[LLM-REQ] target={target_lang_name} n_items={len(texts)} "
         f"items={_trunc(json.dumps(texts, ensure_ascii=False))}")
    t0 = time.time()
    with httpx.Client(timeout=LLM_TIMEOUT) as client:
        resp = client.post(LLM_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    elapsed = time.time() - t0

    content = data["choices"][0]["message"]["content"]
    _log("debug", f"[LLM-RESP] {elapsed:.2f}s content={_trunc(content)}")
    try:
        parsed = json.loads(content)
        items = parsed.get("items")
    except Exception as e:
        _log("info", f"[LLM-RESP] JSON parse failed: {e}; raw={_trunc(content)}")
        raise
    if not isinstance(items, list):
        _log("info", f"[LLM-RESP] 'items' not a list: {type(items).__name__}")
        raise ValueError("LLM response missing 'items' list")
    out = [str(x) for x in items]
    if len(out) != len(texts):
        _log("info", f"[LLM-RESP] COUNT MISMATCH: sent {len(texts)} got {len(out)}")
    _log("debug", f"[LLM-RESP] parsed items={_trunc(json.dumps(out, ensure_ascii=False))}")
    return out


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/translate")
async def translate(request: Request, _=Depends(verify_token)):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
    else:
        data = await request.form()

    text_data = data.get("text")
    target_lang = data.get("target_lang", "ZH")
    tag_handling = request.query_params.get("tag_handling", "plain")

    # Normalize to a list of strings. DeepL accepts text as a string OR an
    # array; we always translate element-wise and return one per element.
    if text_data is None or text_data == "":
        raise HTTPException(status_code=400, detail="Missing text parameter")
    if isinstance(text_data, list):
        texts = [t for t in text_data if t]
        if not texts:
            raise HTTPException(status_code=400, detail="Missing text parameter")
    else:
        texts = [str(text_data)]

    _log("debug", f"[REQ] tag_handling={tag_handling} target_lang={target_lang} "
         f"n_texts={len(texts)}")
    for i, t in enumerate(texts):
        _log("debug", f"[REQ] text[{i}]={_trunc(t)}")

    target_name = bridge.map_target_lang(target_lang)

    translations: List[str] = []
    for idx, text in enumerate(texts):
        if tag_handling == "html":
            translated = bridge.translate_html(text, target_lang)
        else:
            translated = bridge.translate_plain(text, target_lang)
        _log("debug", f"[RESP] text[{idx}] -> {_trunc(translated)}")
        translations.append(translated)

    return {
        "translations": [
            {"detected_source_language": "EN", "text": t} for t in translations
        ]
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
