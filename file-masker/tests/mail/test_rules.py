import pytest

from file_masker.mail.rules import MAX_RULES, RulesError, TenantRules, validate
from file_masker.mail.state import StateDb

GOOD = [{"source": " Zhang San ", "target": "CLIENT-A"}, {"source": "张三", "target": "CLIENT-B"}]


@pytest.fixture
def rules(tmp_path):
    return TenantRules(StateDb(tmp_path / "state.db"))


def test_starts_empty_per_tenant(rules):
    assert rules.snapshot("lq") == () and rules.version("lq") == "sha256:" + "0" * 12


def test_replace_snapshot_and_version(rules):
    saved = rules.replace("lq", GOOD)
    assert saved == (("Zhang San", "CLIENT-A"), ("张三", "CLIENT-B"))
    assert rules.snapshot("lq") == saved
    v1 = rules.version("lq")
    assert v1.startswith("sha256:") and len(v1) == len("sha256:") + 12 and v1 != rules.version("look")
    rules.replace("lq", [{"source": "Zhang San", "target": "CLIENT-A"}])
    assert rules.version("lq") != v1
    rules.replace("lq", GOOD)
    assert rules.version("lq") == v1  # 内容相同 → 版本相同


def test_tenants_are_isolated(rules):
    rules.replace("lq", GOOD)
    rules.replace("look", [{"source": "Li Si", "target": "X"}])
    assert rules.snapshot("lq") == (("Zhang San", "CLIENT-A"), ("张三", "CLIENT-B"))
    assert rules.snapshot("look") == (("Li Si", "X"),)


def test_persists_across_instances(tmp_path):
    TenantRules(StateDb(tmp_path / "state.db")).replace("lq", GOOD)
    assert TenantRules(StateDb(tmp_path / "state.db")).snapshot("lq") == (("Zhang San", "CLIENT-A"), ("张三", "CLIENT-B"))


def test_empty_target_and_clear_are_allowed(rules):
    assert rules.replace("lq", [{"source": "Zhang San", "target": ""}]) == (("Zhang San", ""),)
    assert rules.replace("lq", []) == () and rules.snapshot("lq") == ()


@pytest.mark.parametrize("items,msg", [
    ("not a list", "list"),
    ([{"source": 1, "target": "X"}], "text"),
    ([{"source": "   ", "target": "X"}], "empty"),
    ([{"source": "Li", "target": "X"}], "at least 3"),
    ([{"source": "1 2", "target": "X"}], "at least 3"),
    ([{"source": "张", "target": "X"}], "at least 3"),
    ([{"source": "Zhang San", "target": "A"}, {"source": "zhang san", "target": "B"}], "Duplicate"),
    ([{"source": f"keyword{i:03d}", "target": "X"} for i in range(MAX_RULES + 1)], f"At most {MAX_RULES}"),
    ([{"source": "ACME", "target": "ACME-X"}], "contains"),
    ([{"source": "ACME", "target": "X"}, {"source": "Beta Co", "target": "acme 1"}], "contains"),
])
def test_invalid_rules_are_rejected_and_keep_current(rules, items, msg):
    rules.replace("lq", GOOD)
    with pytest.raises(RulesError, match=msg):
        rules.replace("lq", items)
    assert rules.snapshot("lq") == (("Zhang San", "CLIENT-A"), ("张三", "CLIENT-B"))


def test_validate_is_still_exposed():
    assert validate([{"source": "Zhang San", "target": "A"}]) == (("Zhang San", "A"),)
