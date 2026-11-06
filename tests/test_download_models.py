"""LOOK-5231：默认只下 knowledgator；HanLP（模型非商用）要显式 --with-hanlp。"""
from __future__ import annotations

import sys

import pytest

from scripts import download_models


@pytest.fixture()
def calls(monkeypatch):
    seen: list[str] = []
    for name in ("hanlp", "knowledgator", "nvidia"):
        monkeypatch.setattr(download_models, f"download_{name}", lambda target, n=name: seen.append(n))
    return seen


def _run(monkeypatch, tmp_path, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["download_models.py", "--target", str(tmp_path), *args])
    return download_models.main()


def test_default_downloads_knowledgator_only(monkeypatch, tmp_path, calls):
    assert _run(monkeypatch, tmp_path) == 0
    assert calls == ["knowledgator"]


def test_with_hanlp_adds_hanlp(monkeypatch, tmp_path, calls):
    assert _run(monkeypatch, tmp_path, "--with-hanlp") == 0
    assert calls == ["hanlp", "knowledgator"]


@pytest.mark.parametrize("only", ["hanlp", "knowledgator", "nvidia"])
def test_only_downloads_one(monkeypatch, tmp_path, calls, only):
    assert _run(monkeypatch, tmp_path, "--only", only) == 0
    assert calls == [only]
