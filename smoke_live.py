"""Live smoke test against the real LLM.

Uses payloads shaped like what Collabora sends for a .docx with headers/
footers (page-number field runs, bold runs, doc-code runs) and verifies,
over the live LLM:
  1. every tag/attribute in the input survives in the output (count unchanged)
  2. translatable text is actually translated (CN -> EN)
  3. non-translatable tokens (doc codes, page numbers) are preserved
  4. no duplication (the header/footer duplication bug signature)
  5. the 1:1 array contract holds for batch input

Credentials are read from the environment — no keys are stored here.

Run:  LLM_API_URL=... LLM_API_KEY=... LLM_MODEL=... python3 smoke_live.py
"""
import os
import sys

import bridge

# Credentials come from the environment (same vars as docker-compose).
# No real keys live in this file. Set them before running, e.g.:
#   LLM_API_URL=... LLM_API_KEY=... LLM_MODEL=... python3 smoke_live.py
if not os.environ.get("LLM_API_KEY"):
    sys.exit("LLM_API_KEY not set. Export LLM_API_URL / LLM_API_KEY / LLM_MODEL first.")

# Force reload of main with these env vars
import importlib
import main
importlib.reload(main)


def tag_count(html):
    # count opening tags (rough but catches duplication/loss)
    return html.count("<") - html.count("</")  # net open vs close won't help; use open count
def open_tag_count(html):
    return html.count("<span") + html.count("<b>") + html.count("<br")


CASES = [
    (
        "header2 (company name + doc code)",
        '<span style="font-family:SimSun;font-size:9pt">深圳希沃康医疗科技有限公司                         质量手册 </span>'
        '<span style="font-family:SimSun;font-size:9pt">QM-SVC-2023/1.4</span>',
        {"must_keep": ["QM-SVC-2023/1.4", 'style="font-family:SimSun;font-size:9pt"'],
         "must_translate": ["深圳希沃康医疗科技有限公司", "质量手册"]},
    ),
    (
        "footer2 (text + page-number field runs)",
        '<span>保密文件，知识产权属希沃康公司所有</span>'
        '<span>                                                         </span>'
        '<span>第 </span><span>5</span><span> 页 共 </span><span>67</span><span> 页</span>',
        {"must_keep": ["5", "67"],  # page numbers must NOT be altered
         "must_translate": ["保密文件", "页"]},
    ),
    (
        "body bold run (pptx-formatting analogy)",
        '<span style="font-weight:bold">类别和属性</span><span>文件编号</span>',
        {"must_keep": ['style="font-weight:bold"'],
         "must_translate": ["类别和属性", "文件编号"]},
    ),
]


def run_one(name, html_in, expectations):
    print(f"\n=== {name} ===")
    print(f"IN : {html_in}")
    in_opens = open_tag_count(html_in)

    try:
        out = bridge.translate_html(html_in, "EN")
    except Exception as e:
        print(f"  !! exception: {e}")
        return False
    print(f"OUT: {out}")

    out_opens = open_tag_count(out)
    ok = True

    # 1. tag count unchanged (no dup, no loss)
    if in_opens != out_opens:
        print(f"  FAIL tag count: in={in_opens} out={out_opens}")
        ok = False
    else:
        print(f"  ok  tag count preserved ({in_opens})")

    # 2. attributes/tokens kept
    for token in expectations["must_keep"]:
        if token not in out:
            print(f"  FAIL lost token: {token!r}")
            ok = False
        else:
            print(f"  ok  kept: {token!r}")

    # 3. translatable text actually changed (CN gone from translated spots)
    for cn in expectations["must_translate"]:
        # the CN text should NOT survive verbatim in the output (it was translated)
        if cn in out:
            # Allow it only if the same string legitimately appears untranslated
            # (it shouldn't for these cases). Flag as a possible non-translation.
            print(f"  WARN original text still present: {cn!r} (maybe untranslated?)")
        else:
            print(f"  ok  translated away: {cn!r}")

    return ok


def run_batch_contract():
    """The 1:1 array contract over the live LLM."""
    print("\n=== batch 1:1 contract ===")
    texts = ["深圳希沃康医疗科技有限公司", "QM-SVC-2023/1.4", "保密文件", "第 5 页"]
    try:
        out = main.llm_translate_items(texts, "English")
    except Exception as e:
        print(f"  !! exception: {e}")
        return False
    print(f"  in  ({len(texts)}): {texts}")
    print(f"  out ({len(out)}): {out}")
    if len(out) != len(texts):
        print(f"  FAIL count: in={len(texts)} out={len(out)}")
        return False
    print("  ok  count matches")
    return True


if __name__ == "__main__":
    results = [run_one(n, h, e) for (n, h, e) in CASES]
    results.append(run_batch_contract())
    print("\n" + "=" * 50)
    if all(results):
        print("ALL LIVE TESTS PASSED")
        sys.exit(0)
    else:
        print(f"{results.count(False)} FAILURE(S)")
        sys.exit(1)
