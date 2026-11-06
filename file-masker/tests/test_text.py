from file_masker.engine import build_rules, find_hits, match_line
from file_masker.text import find_residuals, mask_segments, mask_text

RULES = build_rules({"Zhang San": "CLIENT-A", "张三": "CLIENT-B"})


def test_same_semantics_as_pdf_engine():
    out, hits = mask_text("client zhang  san / 张 三", RULES)
    assert out == "client CLIENT-A / CLIENT-B"
    assert hits == 2
    assert find_residuals(out, RULES) == 0


def test_longer_rule_wins_and_hits_do_not_overlap():
    rules = build_rules({"Zhang San": "P-A", "Zhang San Ltd": "CO-A"})
    out, hits = mask_text("Zhang San Ltd and Zhang San", rules)
    assert out == "CO-A and P-A"
    assert hits == 2


def test_keyword_across_line_break_is_masked():
    out, _ = mask_text("Dear Zhang\nSan,", RULES)
    assert out == "Dear CLIENT-A,"


def test_empty_target_deletes_keyword():
    out, _ = mask_text("to Zhang San.", build_rules({"Zhang San": ""}))
    assert out == "to ."


def test_no_hit_returns_text_unchanged():
    assert mask_text("nothing here", RULES) == ("nothing here", 0)
    assert mask_text("Zhang San", []) == ("Zhang San", 0)


def test_find_residuals_counts_remaining_keywords():
    assert find_residuals("Zhang San and 张三", RULES) == 2
    assert find_residuals("CLIENT-A", RULES) == 0


def test_match_line_alias_kept():
    assert match_line is find_hits


def test_segments_keyword_split_across_segments():
    # Outlook 常把一个名字拆进相邻的 <span>：替换文字写进命中起点所在段，其余命中字符删除
    out, hits = mask_segments(["Dear ", "Zhang", " San", ", hi"], RULES)
    assert out == ["Dear ", "CLIENT-A", "", ", hi"]
    assert hits == 1


def test_segments_keep_boundaries_without_hits():
    assert mask_segments(["a", "", "b"], RULES) == (["a", "", "b"], 0)


def test_segments_multiple_hits_in_one_segment():
    out, hits = mask_segments(["Zhang San and 张", "三 end"], RULES)
    assert out == ["CLIENT-A and CLIENT-B", " end"]
    assert hits == 2
