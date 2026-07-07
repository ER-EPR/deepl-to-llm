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


# ---------------------------------------------------------------------------
# batch_texts  (high-threshold safety net — context is prioritized)
# ---------------------------------------------------------------------------

class TestBatchTexts:
    """batch_texts is a SAFETY NET, not a context-chopper. Real Collabora
    paragraphs are small and translate in 1-2.5s with thinking disabled, so
    they must NEVER be split — cross-sentence context is what makes LLM
    translation accurate. Splitting only kicks in for pathological payloads
    (hundreds of text nodes in one fragment) that could blow the budget.
    Tests therefore pass EXPLICIT small thresholds to exercise the splitting
    logic; the defaults (500 items / 12000 chars) leave normal input alone."""

    def test_normal_paragraph_never_split_with_defaults(self):
        # a realistic paragraph: a handful of items, well under default limits
        texts = ["本条款规定医疗器械质量管理体系要求"] * 8
        batches = bridge.batch_texts(texts)  # defaults: 500 items / 12000 chars
        assert batches == [texts]  # one batch, context intact

    def test_small_list_is_one_batch(self):
        texts = ["a", "b", "c"]
        batches = bridge.batch_texts(texts, max_items=60, max_chars=600)
        assert batches == [["a", "b", "c"]]

    def test_splits_by_max_items(self):
        texts = [f"t{i}" for i in range(150)]
        batches = bridge.batch_texts(texts, max_items=60, max_chars=100000)
        # 150 / 60 -> 3 batches (60, 60, 30)
        assert len(batches) == 3
        assert sum(len(b) for b in batches) == 150
        assert all(len(b) <= 60 for b in batches)

    def test_splits_by_max_chars(self):
        # each item 30 chars; max_chars=100 -> ~3 per batch
        texts = [f"x{'.'*28}" for _ in range(10)]  # 10 items x 30 chars = 300
        batches = bridge.batch_texts(texts, max_items=100, max_chars=100)
        for b in batches:
            assert sum(len(t) for t in b) <= 100
        assert sum(len(b) for b in batches) == 10

    def test_preserves_order_across_batches(self):
        texts = [f"item{i}" for i in range(200)]
        batches = bridge.batch_texts(texts, max_items=60, max_chars=100000)
        flat = [t for b in batches for t in b]
        assert flat == texts  # order preserved, nothing lost/duplicated

    def test_single_huge_item_stays_in_own_batch(self):
        # one item bigger than max_chars must not be dropped or split mid-string;
        # it gets its own batch (translator handles it, or that batch times out
        # and falls back — but batching must not corrupt it)
        big = "x" * 5000
        batches = bridge.batch_texts([big, "small"], max_items=60, max_chars=600)
        assert batches[0] == [big]
        assert batches[1] == ["small"]

    def test_empty_input(self):
        assert bridge.batch_texts([], max_items=60, max_chars=600) == []


class TestBatchedTranslationContract:
    """translate_html uses batch_texts as a safety net: a normal fragment is
    one translator call (context preserved); only a pathological fragment
    splits. Results reassemble in order. Any batch raising -> fall back to
    the original HTML untranslated (no partial/corrupted output)."""

    def test_normal_html_is_single_call_context_preserved(self):
        # realistic paragraph -> one call, context not split
        html_in = '<span>本条款规定医疗器械质量管理体系要求</span>'
        calls = []
        def recording_translate(texts, target_lang):
            calls.append(list(texts))
            return [f"T{t}" for t in texts]
        bridge.translate_html(html_in, "EN", translate_fn=recording_translate)
        assert len(calls) == 1  # context preserved, no splitting

    def test_pathological_html_translated_in_batches_in_order(self):
        # 150 spans -> splits, but result identical to a single call.
        html_in = "".join(f"<span>n{i}</span>" for i in range(150))

        calls = []
        def recording_translate(texts, target_lang):
            calls.append(list(texts))
            return [f"T{t}" for t in texts]

        out = bridge.translate_html(
            html_in, "EN", translate_fn=recording_translate,
            max_items=60, max_chars=100000,  # force split for this test
        )
        assert len(calls) > 1  # multiple batches
        total = sum(len(c) for c in calls)
        assert total == 150
        for i in range(150):
            assert f"Tn{i}" in out
        assert out.count("<span") == 150

    def test_batch_failure_falls_back_to_original(self):
        # if any sub-batch translator call raises, the whole fragment falls
        # back to untranslated (no partial/corrupted output). Fail on the
        # 2nd batch onward; force splitting with explicit small thresholds.
        html_in = "".join(f"<span>n{i}</span>" for i in range(150))
        call_count = {"n": 0}
        def failing_translate(texts, target_lang):
            call_count["n"] += 1
            if call_count["n"] >= 2:  # 2nd batch onward raises
                raise RuntimeError("timeout")
            return [f"T{t}" for t in texts]
        out = bridge.translate_html(
            html_in, "EN", translate_fn=failing_translate,
            max_items=60, max_chars=100000,
        )
        assert out == html_in  # unchanged, safe
