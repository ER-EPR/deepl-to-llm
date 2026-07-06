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

from typing import Callable, List, Optional

from lxml import etree, html as lxml_html

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


def translate_html(
    html_str: str,
    target_lang: str,
    translate_fn: Optional[TranslateFn] = None,
) -> str:
    """Translate an HTML fragment while preserving all markup.

    If translate_fn is None, the default LLM-backed translator is used
    (imported lazily so tests don't need network).

    On any structural problem (translation count mismatch, parse failure),
    returns the original html_str UNTRANSLATED. Returning the source
    untouched is strictly better than returning corrupted markup: an
    untranslated paragraph is a quality regression, a duplicated one is a
    data-corruption incident.
    """
    try:
        texts = extract_text_nodes(html_str)
    except Exception:
        return html_str
    if not texts:
        return html_str

    if translate_fn is None:
        translate_fn = _default_llm_translate

    try:
        translated = translate_fn(texts, map_target_lang(target_lang))
    except Exception:
        return html_str

    if len(translated) != len(texts):
        return html_str

    try:
        return refill_text_nodes(html_str, translated)
    except Exception:
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
        out = translate_fn([text], map_target_lang(target_lang))
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
