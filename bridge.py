"""DeepL-to-LLM bridge core logic.

Replaces what real DeepL's `tag_handling=html` does: parse the HTML, extract
the translatable text, send ONLY the text to an LLM, then put the translated
text back into the original markup without touching a single tag or attribute.

This exists because feeding raw HTML to an LLM ("please preserve the tags")
is unreliable: the model drops attributes, duplicates tags, merges runs.
For Collabora's whole-document translation loop, a single corrupted paragraph
cascades into runaway duplication (Bug 1). Keeping the LLM away from markup
removes that failure mode at the root.

Public API:
  extract_text_nodes(html) -> list[str]
  refill_text_nodes(html, translations) -> str
  translate_html(html, target_lang, translate_fn=None) -> str
  translate_plain(text, target_lang, translate_fn=None) -> str
  map_target_lang(code) -> str
  LANG_CODE_MAP
"""
from __future__ import annotations

import os
import sys
from typing import Callable, List, Optional

from lxml import etree, html as lxml_html


def _blog(level: str, msg: str) -> None:
    """Log to stderr if LOG_LEVEL permits. Mirrors main._log.

    Levels: debug < info < off. LOG_LEVEL=debug shows everything; info (or
    unset) shows warnings/fallbacks; anything else is silent. Goes to stderr
    so docker logs captures it.
    """
    cfg = os.getenv("LOG_LEVEL", "info").lower()
    levels = {"debug": 0, "info": 1}
    if cfg not in levels:
        return
    if levels.get(level, 99) < levels[cfg]:
        return
    print(msg, file=sys.stderr, flush=True)


def _btrunc(s: str, n: int = 800) -> str:
    if s is None:
        return "<None>"
    if len(s) <= n:
        return s
    return f"{s[:n//2]}…[{len(s)} chars]…{s[-n//4:]}"

# DeepL language codes -> human-readable name for the LLM prompt.
# DeepL accepts uppercase codes like ZH, EN-US, ZH-HANS. The LLM translates
# better from a name than from "ZH".
LANG_CODE_MAP = {
    "ZH": "Chinese (Simplified)",
    "ZH-HANS": "Chinese (Simplified)",
    "ZH-HANT": "Chinese (Traditional)",
    "EN": "English",
    "EN-US": "English (US)",
    "EN-GB": "English (UK)",
    "JA": "Japanese",
    "KO": "Korean",
    "FR": "French",
    "DE": "German",
    "ES": "Spanish",
    "ES-419": "Spanish (Latin America)",
    "IT": "Italian",
    "PT": "Portuguese",
    "PT-BR": "Portuguese (Brazil)",
    "PT-PT": "Portuguese (Portugal)",
    "RU": "Russian",
    "NL": "Dutch",
    "PL": "Polish",
    "AR": "Arabic",
    "BG": "Bulgarian",
    "CS": "Czech",
    "DA": "Danish",
    "EL": "Greek",
    "ET": "Estonian",
    "FI": "Finnish",
    "HU": "Hungarian",
    "LT": "Lithuanian",
    "LV": "Latvian",
    "RO": "Romanian",
    "SK": "Slovak",
    "SL": "Slovenian",
    "SV": "Swedish",
    "TR": "Turkish",
    "UK": "Ukrainian",
}


def map_target_lang(code: Optional[str]) -> str:
    """Map a DeepL target_lang code to a human-readable language name.

    Unknown codes fall back to the code itself rather than empty, so the
    prompt still names a target. Input is normalized to uppercase and the
    primary subtag is matched (e.g. 'EN-us' -> 'EN-US' -> 'English (US)',
    but a wholly unknown 'XX-YY' returns 'XX-YY').
    """
    if not code:
        return "the target language"
    norm = code.strip().upper()
    if norm in LANG_CODE_MAP:
        return LANG_CODE_MAP[norm]
    # try primary subtag (e.g. unknown 'EN-ZZ' -> still English)
    primary = norm.split("-")[0]
    if primary in LANG_CODE_MAP:
        return LANG_CODE_MAP[primary]
    return norm


# ---------------------------------------------------------------------------
# HTML structure preservation
# ---------------------------------------------------------------------------

# Tags whose content is NOT translatable (code, script, field artifacts).
# Real DeepL has ignore_tags; we hardcode the common ones.
_IGNORE_TAGS = {"script", "style", "code", "pre", "kbd", "samp"}


def _is_ignore(element) -> bool:
    tag = etree.QName(element.tag).localname if isinstance(element.tag, str) else None
    return tag in _IGNORE_TAGS


_WRAPPER_TAG = "div"


def _parse_fragment(html_str: str):
    """Parse an HTML fragment WITHOUT injecting a wrapper element.

    lxml's fromstring() wraps multi-root fragments in a <span>, which would
    change the tag count and corrupt Collabora's document (the exact
    duplication bug we're preventing). We parse inside a throwaway <div>,
    then return (wrapper, children) so callers can mutate the children and
    reserialize them directly — never the wrapper.
    """
    wrapper = lxml_html.fromstring(f"<{_WRAPPER_TAG}>{html_str}</{_WRAPPER_TAG}>")
    return wrapper


def _serialize_fragment(wrapper) -> str:
    """Serialize a fragment's children back to HTML, dropping the wrapper."""
    return "".join(
        lxml_html.tostring(child, encoding="unicode") for child in wrapper
    )


def _collect_text(tree) -> List[tuple]:
    """Walk the tree in document order and collect translatable text slots.

    Returns a list of (kind, owner) where kind is 'text' or 'tail' and owner
    is the element/node that holds it. Whitespace-only strings are skipped
    (they carry no translatable content and skipping keeps run boundaries
    stable for things like the spaces between field-code runs).
    """
    slots: List[tuple] = []
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        if _is_ignore(el):
            continue
        if el.text and el.text.strip():
            slots.append(("text", el))
        # tail = text following this element, still inside its parent.
        # Skip tail on ignored elements (their text already skipped).
        if el.tail and el.tail.strip():
            # tail belongs to parent context; only skip if parent is ignored
            parent = el.getparent()
            if parent is None or not _is_ignore(parent):
                slots.append(("tail", el))
    return slots


def extract_text_nodes(html_str: str) -> List[str]:
    """Extract translatable text strings from HTML, in document order.

    Whitespace-only fragments are skipped. The returned list is what gets
    sent to the LLM; refill_text_nodes consumes a same-length list back.
    """
    wrapper = _parse_fragment(html_str)
    out: List[str] = []
    for kind, owner in _collect_text(wrapper):
        out.append(owner.text if kind == "text" else owner.tail)
    return out


def refill_text_nodes(html_str: str, translations: List[str]) -> str:
    """Put translated text back into the markup, preserving everything else.

    Raises ValueError if len(translations) != number of extracted text slots,
    because misalignment would silently corrupt the document (the exact
    failure we're protecting against).
    """
    wrapper = _parse_fragment(html_str)
    slots = _collect_text(wrapper)
    if len(slots) != len(translations):
        raise ValueError(
            f"translation count mismatch: got {len(translations)} "
            f"for {len(slots)} text nodes"
        )
    for (kind, owner), translated in zip(slots, translations):
        if kind == "text":
            owner.text = translated
        else:
            owner.tail = translated
    return _serialize_fragment(wrapper)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# Type of the translator callable: takes (texts, target_lang_name) -> list[str].
TranslateFn = Callable[[List[str], str], List[str]]

# Per-batch safety-net limits. Context is prioritized: real Collabora
# paragraphs are small (one or a few text nodes) and translate in 1-2.5s
# with thinking disabled, so they must NEVER be split — splitting would
# cost the cross-sentence context that makes LLM translation accurate.
# These high thresholds only catch pathological payloads (e.g. a 150-span
# table exported as a single fragment) that would otherwise exceed the
# LLM/timeout budget. A real paragraph never reaches 5000 chars / 500 nodes.
MAX_ITEMS_PER_BATCH = 500
MAX_CHARS_PER_BATCH = 12000


def batch_texts(
    texts: List[str],
    max_items: int = MAX_ITEMS_PER_BATCH,
    max_chars: int = MAX_CHARS_PER_BATCH,
) -> List[List[str]]:
    """Split a list of text strings into sub-batches, as a SAFETY NET only.

    Context is prioritized over fitting any time window: the thresholds are
    deliberately high so normal paragraphs are never split (they translate in
    1-2.5s with thinking disabled). Only pathological payloads — a single
    HTML fragment with hundreds of text nodes — get split, and only because
    a single call for them could exceed the LLM/timeout budget. A single text
    larger than max_chars gets its own batch (it is NOT split mid-string).

    Order is preserved end-to-end: flattening the returned batches reproduces
    the input exactly.
    """
    if not texts:
        return []
    batches: List[List[str]] = []
    current: List[str] = []
    current_chars = 0
    for t in texts:
        item_chars = len(t)
        would_overflow_items = len(current) >= max_items
        would_overflow_chars = current and (current_chars + item_chars) > max_chars
        if would_overflow_items or would_overflow_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(t)
        current_chars += item_chars
    if current:
        batches.append(current)
    return batches


def _translate_batched(
    texts: List[str],
    target_lang_name: str,
    translate_fn: TranslateFn,
    max_items: int = MAX_ITEMS_PER_BATCH,
    max_chars: int = MAX_CHARS_PER_BATCH,
) -> List[str]:
    """Translate a list of texts in sub-batches, concatenating results in order.

    Raises on any batch failure so the caller can fall back to untranslated.
    The thresholds default to the high safety-net values; callers may override
    (tests use small values to force splitting).
    """
    out: List[str] = []
    for batch in batch_texts(texts, max_items=max_items, max_chars=max_chars):
        out.extend(translate_fn(batch, target_lang_name))
    return out


def translate_html(
    html_str: str,
    target_lang: str,
    translate_fn: Optional[TranslateFn] = None,
    max_items: int = MAX_ITEMS_PER_BATCH,
    max_chars: int = MAX_CHARS_PER_BATCH,
) -> str:
    """Translate an HTML fragment while preserving all markup.

    If translate_fn is None, the default LLM-backed translator is used
    (imported lazily so tests don't need network).

    The extracted text nodes are translated in sub-batches only as a SAFETY
    NET (see batch_texts): normal paragraphs are a single call so their
    context is preserved; only pathological payloads split. max_items/max_chars
    override the high defaults for testing.

    On any structural problem (translation count mismatch, parse failure,
    any batch raising), returns the original html_str UNTRANSLATED.
    Returning the source untouched is strictly better than returning corrupted
    markup: an untranslated paragraph is a quality regression, a duplicated
    one is a data-corruption incident.
    """
    try:
        texts = extract_text_nodes(html_str)
    except Exception as e:
        _blog("info", f"[HTML] parse failed: {e!r}; html={_btrunc(html_str)}")
        return html_str
    _blog("debug", f"[HTML] extracted {len(texts)} nodes from "
          f"{_btrunc(html_str)}: {_btrunc(repr(texts))}")
    if not texts:
        return html_str

    if translate_fn is None:
        translate_fn = _default_llm_translate

    try:
        translated = _translate_batched(
            texts, map_target_lang(target_lang), translate_fn,
            max_items=max_items, max_chars=max_chars,
        )
    except Exception as e:
        _blog("info", f"[HTML] translate_fn raised: {e!r}; falling back to original")
        return html_str

    if len(translated) != len(texts):
        _blog("info", f"[HTML] count mismatch: extracted {len(texts)} "
              f"got {len(translated)}; falling back to original")
        return html_str

    try:
        out = refill_text_nodes(html_str, translated)
        _blog("debug", f"[HTML] refilled OK: {_btrunc(out)}")
        return out
    except Exception as e:
        _blog("info", f"[HTML] refill failed: {e!r}; falling back to original")
        return html_str


def translate_plain(
    text: str,
    target_lang: str,
    translate_fn: Optional[TranslateFn] = None,
) -> str:
    """Translate a single plain-text string (no markup)."""
    if translate_fn is None:
        translate_fn = _default_llm_translate
    try:
        out = _translate_batched([text], map_target_lang(target_lang), translate_fn)
    except Exception:
        return text
    if len(out) == 1:
        return out[0]
    return text


def _default_llm_translate(texts: List[str], target_lang_name: str) -> List[str]:
    """Call the configured LLM with a JSON-structured-output contract.

    The LLM receives a JSON object {"items": [...]} and MUST return
    {"items": [...]} with the same length, in order. JSON mode + a hard
    instruction makes the 1:1 contract enforceable; if the model still
    misbehaves, translate_html falls back to the original.
    """
    from main import llm_translate_items  # lazy; avoids circular import at module load
    return llm_translate_items(texts, target_lang_name)
