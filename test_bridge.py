"""Tests for the DeepL-to-LLM bridge core logic.

The bridge must do what real DeepL's tag_handling=html does:
parse HTML, translate ONLY the text inside tags, and preserve every tag,
attribute, and structural detail byte-for-byte. An LLM must never see or
touch the markup itself.

Tests here cover the pure (network-free) functions in bridge.py:
  - extract_text_nodes(html) -> list[str]   (text to translate)
  - refill_text_nodes(html, translations) -> str  (reassemble)
  - map_target_lang(code) -> str            (DeepL code -> human name)
  - translate_plain(text, target_lang) contract

Network-dependent behavior (the actual LLM call) is exercised separately
in a smoke test, not here.
"""
import pytest

import bridge


# ---------------------------------------------------------------------------
# extract_text_nodes / refill_text_nodes  (the structure-preservation core)
# ---------------------------------------------------------------------------

class TestExtractTextNodes:
    def test_single_span(self):
        html = '<span>Hello</span>'
        assert bridge.extract_text_nodes(html) == ["Hello"]

    def test_multiple_spans(self):
        html = '<span>Hello</span><span>World</span>'
        assert bridge.extract_text_nodes(html) == ["Hello", "World"]

    def test_nested_tags_text_collected(self):
        # text lives under nested inline tags too
        html = '<p>Before <b>bold</b> after</p>'
        texts = bridge.extract_text_nodes(html)
        assert "Before " in texts
        assert "bold" in texts
        assert " after" in texts

    def test_empty_text_ignored(self):
        html = '<span>real</span><span>   </span><span></span>'
        assert bridge.extract_text_nodes(html) == ["real"]

    def test_preserves_order(self):
        html = '<span>A</span><span>B</span><span>C</span>'
        assert bridge.extract_text_nodes(html) == ["A", "B", "C"]


class TestRefillTextNodes:
    def test_one_to_one_replacement(self):
        html = '<span>Hello</span><span>World</span>'
        out = bridge.refill_text_nodes(html, ["你好", "世界"])
        assert "你好" in out
        assert "世界" in out
        assert "Hello" not in out
        assert "World" not in out

    def test_preserves_attributes(self):
        # style/attributes must survive untouched
        html = '<span style="font-weight:bold;font-size:24pt">Hello</span>'
        out = bridge.refill_text_nodes(html, ["你好"])
        assert 'style="font-weight:bold;font-size:24pt"' in out
        assert "你好" in out
        assert "Hello" not in out

    def test_preserves_tag_structure(self):
        # nested structure: outer + inner spans, only text changes
        html = '<span class="hdr"><span>Page</span> <span>5</span></span>'
        out = bridge.refill_text_nodes(html, ["页", "5"])
        # both inner texts replaced, structural tags + class attr intact
        assert 'class="hdr"' in out
        assert "页" in out
        assert "5" in out
        assert "Page" not in out

    def test_preserves_br_and_entities(self):
        html = '<span>Line1</span><br><span>Line2</span>'
        out = bridge.refill_text_nodes(html, ["第一行", "第二行"])
        assert "<br>" in out
        assert "第一行" in out
        assert "第二行" in out

    def test_count_mismatch_raises(self):
        # the contract: caller guarantees len(translations)==len(extracted).
        # refill must refuse to silently misalign.
        html = '<span>A</span><span>B</span>'
        with pytest.raises(ValueError):
            bridge.refill_text_nodes(html, ["只有一条"])

    def test_round_trip_preserves_tag_count(self):
        # the Bug-1 root cause: tag count must NOT grow.
        html = '<span>A</span><span>B</span><span>C</span>'
        out = bridge.refill_text_nodes(html, ["甲", "乙", "丙"])
        assert out.count("<span") == html.count("<span")
        assert out.count("</span>") == html.count("</span>")


class TestTranslateHtmlContract:
    """The HTML translate path: parse -> extract -> (translate) -> refill.
    We test the orchestration with a fake translator so no network is needed.
    """

    def test_translate_html_preserves_structure_with_fake_translator(self):
        html_in = (
            '<span style="font-weight:bold">深圳希沃康医疗科技有限公司</span>'
            '<span>QM-SVC-2023/1.4</span>'
        )
        # fake translator: appends "[T]" to each text, simulating an LLM whose
        # output we DON'T fully trust to touch markup
        def fake_translate(texts, target_lang):
            return [t + "[T]" for t in texts]

        out = bridge.translate_html(html_in, "EN", translate_fn=fake_translate)
        # attributes preserved
        assert 'style="font-weight:bold"' in out
        # both texts translated (Chinese text + marker, code + marker)
        assert "深圳希沃康医疗科技有限公司[T]" in out
        assert "QM-SVC-2023/1.4[T]" in out
        # the bare original text (not followed by [T]) must NOT appear —
        # i.e. every occurrence was translated
        assert "深圳希沃康医疗科技有限公司</span>" not in out
        assert "QM-SVC-2023/1.4</span>" not in out
        # tag count unchanged (no duplication)
        assert out.count("<span") == html_in.count("<span")

    def test_translate_html_falls_back_when_count_mismatched(self):
        # if the LLM returns wrong number of translations, the bridge must
        # NOT corrupt the document — return the original HTML untranslated.
        html_in = '<span>A</span><span>B</span>'
        def bad_translate(texts, target_lang):
            return ["only one"]  # wrong count

        out = bridge.translate_html(html_in, "EN", translate_fn=bad_translate)
        assert out == html_in  # fallback to original, structure intact


# ---------------------------------------------------------------------------
# map_target_lang  (DeepL language code -> human-readable name)
# ---------------------------------------------------------------------------

class TestMapTargetLang:
    def test_zh(self):
        assert "Chinese" in bridge.map_target_lang("ZH")

    def test_zh_hans(self):
        assert "Chinese" in bridge.map_target_lang("ZH-HANS")

    def test_en_us(self):
        assert "English" in bridge.map_target_lang("EN-US")

    def test_en(self):
        assert "English" in bridge.map_target_lang("EN")

    def test_unknown_falls_back(self):
        # unknown code returns the code itself, never an empty string,
        # so the prompt still has *something* to translate into
        assert bridge.map_target_lang("XX-YY") == "XX-YY"

    def test_lowercase_normalized(self):
        # DeepL codes are uppercase, but be defensive
        assert "English" in bridge.map_target_lang("en")
