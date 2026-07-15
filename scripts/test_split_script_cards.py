#!/usr/bin/env python3
"""Unit tests for split_script_to_cards (ASCII 句点支持).

科普风格 (CLAUSE_RULES) 教 LLM 用 ASCII "." 收句, 旧 regex 只认全角
。！？ → 整篇被当 1 句, 19 个 chunk 只有 1 个非空, 配图只生成 1 张
(v_1aed9b49)。

Run: python3 scripts/test_split_script_cards.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rv  # noqa: E402


def test_fullwidth_periods_split():
    """全角句号稿子照常切分 (回归)。"""
    script = "第一句话。第二句话。第三句话。第四句话。"
    chunks = rv.split_script_to_cards(script, n_cards=4)
    non_empty = [c for c in chunks if c.strip()]
    assert len(non_empty) == 4, f"expected 4 non-empty, got {len(non_empty)}: {chunks}"


def test_ascii_periods_split():
    """ASCII '. ' 收句的科普稿必须切出多个句子, 不能整篇算 1 句。"""
    script = "一块光伏板, 一平米, 能发 200 瓦电. 跟一个白炽灯泡差不多. 但你可能没想到, 最忙的不是硅. 是光."
    chunks = rv.split_script_to_cards(script, n_cards=4)
    non_empty = [c for c in chunks if c.strip()]
    assert len(non_empty) == 4, \
        f"ASCII-period script must split into 4 sentences, got {len(non_empty)}: {chunks}"


def test_ascii_decimal_not_split():
    """小数点 (1.15) 不是句子边界, 不切。"""
    script = "语速是 1.15 倍. 挺快的. 也不算太快. 就这样."
    chunks = rv.split_script_to_cards(script, n_cards=4)
    non_empty = [c for c in chunks if c.strip()]
    assert len(non_empty) == 4, f"got {len(non_empty)}: {chunks}"
    assert any("1.15" in c for c in non_empty), \
        f"decimal 1.15 must stay intact: {chunks}"


def test_kepu_real_script_splits():
    """v_1aed9b49 真实稿 (27 个 ASCII 句点) 必须切满 19 个 chunk。"""
    p = Path(__file__).resolve().parents[1] / "runs" / "v_1aed9b49" / "script.txt"
    if not p.exists():
        return  # 真实产物不在则跳过 (fresh checkout)
    script = p.read_text(encoding="utf-8")
    chunks = rv.split_script_to_cards(script, n_cards=19)
    non_empty = [c for c in chunks if c.strip()]
    assert len(non_empty) == 19, \
        f"real kepu script must fill all 19 chunks, got {len(non_empty)}"


def main():
    tests = [
        test_fullwidth_periods_split,
        test_ascii_periods_split,
        test_ascii_decimal_not_split,
        test_kepu_real_script_splits,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
