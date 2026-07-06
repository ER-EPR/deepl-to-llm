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
from typing import List, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request

import bridge

app = FastAPI()

# Config is read at import time (module-level constants -> restart to change).
LLM_API_URL = os.getenv(
    "LLM_API_URL", "https://example.com/v1/chat/completions"
)
LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-xxx")
LLM_MODEL = os.getenv("LLM_MODEL", "prx.free")
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "<your-bridge-token>")

# Collabora's libcurl client uses ~10s. Stay under it so the bridge responds
# before Collabora times out and leaves a paragraph untranslated/empty.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "9.0"))

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
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}

    with httpx.Client(timeout=LLM_TIMEOUT) as client:
        resp = client.post(LLM_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    items = parsed.get("items")
    if not isinstance(items, list):
        raise ValueError("LLM response missing 'items' list")
    return [str(x) for x in items]


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

    target_name = bridge.map_target_lang(target_lang)

    translations: List[str] = []
    for text in texts:
        if tag_handling == "html":
            translated = bridge.translate_html(text, target_lang)
        else:
            translated = bridge.translate_plain(text, target_lang)
        translations.append(translated)

    return {
        "translations": [
            {"detected_source_language": "EN", "text": t} for t in translations
        ]
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
