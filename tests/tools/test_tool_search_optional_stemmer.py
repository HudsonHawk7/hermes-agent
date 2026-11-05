"""Catalog and batch expansion must load when snowballstemmer is absent."""

from __future__ import annotations


def test_stem_without_stemmer_returns_token(monkeypatch):
    import tools.tool_search_catalog as catalog

    monkeypatch.setattr(catalog, "snowballstemmer", None)
    catalog._stem.cache_clear()
    try:
        assert catalog._stem("running") == "running"
        assert catalog._tokenize("running issues") == ["running", "issues"]
    finally:
        catalog._stem.cache_clear()


def test_tool_call_batches_constant_matches_catalog():
    from agent.tool_call_batches import TOOL_CALL_NAME as batch_name
    from tools.tool_search_catalog import TOOL_CALL_NAME as catalog_name

    assert batch_name == catalog_name == "tool_call"
