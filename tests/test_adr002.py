"""Tests for ADR 002 gap resolutions."""
import os

import pytest

from wazi.agent import evidence as ev


def test_configurable_doc_limit_respects_env_var(monkeypatch):
    """WAZI_MAX_DOC_CHARS environment variable controls document text limit."""
    # Default value
    assert ev.MAX_DOC_CHARS == 2000

    # Custom value
    monkeypatch.setenv("WAZI_MAX_DOC_CHARS", "5000")
    # Reload to pick up new env var
    import importlib
    importlib.reload(ev)
    assert ev.MAX_DOC_CHARS == 5000

    # Invalid value falls back to default (empty string treated as default)
    monkeypatch.setenv("WAZI_MAX_DOC_CHARS", "")
    importlib.reload(ev)
    assert ev.MAX_DOC_CHARS == 2000


def test_peer_comparison_signal_downgrade_is_deterministic():
    """Signal downgrade when peer comparison shows typical concentration happens
    for ALL buyer_history signals, not just during iteration."""
    ev_list = [
        {"id": "E1", "kind": "buyer_history", "text": "Signal 1", "signal": True},
        {"id": "E2", "kind": "buyer_history", "text": "Signal 2", "signal": True},
        {"id": "E3", "kind": "price", "text": "Price info", "signal": False},
    ]

    peers_result = {
        "found": True,
        "position": "typical_for_peers",
        "category": "goods",
        "buyer_share": 0.45,
        "buyer_awards": 10,
        "n_peers": 15,
        "peer_median_share": 0.42,
        "peer_p75_share": 0.55,
    }

    ev._add_peers(ev_list, "ocds-test-001", peers_result)

    # Original buyer_history signals should be downgraded
    original_buyer_history = [e for e in ev_list if e["id"] in ("E1", "E2")]
    assert len(original_buyer_history) == 2
    assert all(not e["signal"] for e in original_buyer_history)
    assert all("Downgraded after peer comparison" in e["text"] for e in original_buyer_history)

    # Price item should be unchanged
    price_items = [e for e in ev_list if e["kind"] == "price"]
    assert len(price_items) == 1
    assert not price_items[0]["signal"]

    # A new buyer_history evidence item is added with the peer comparison result
    # This is expected behavior
    new_buyer_history = [e for e in ev_list if e["id"] not in ("E1", "E2", "E3") and e["kind"] == "buyer_history"]
    assert len(new_buyer_history) == 1
    assert "within the range of peers" in new_buyer_history[0]["text"]


def test_peer_comparison_signal_stays_when_above_peers():
    """Signal stays when peer comparison shows concentration above peers."""
    ev_list = [
        {"id": "E1", "kind": "buyer_history", "text": "Signal 1", "signal": True},
    ]
    
    peers_result = {
        "found": True,
        "position": "above_peers",
        "category": "goods",
        "buyer_share": 0.85,
        "buyer_awards": 10,
        "n_peers": 15,
        "peer_median_share": 0.42,
        "peer_p75_share": 0.55,
    }
    
    ev._add_peers(ev_list, "ocds-test-001", peers_result)
    
    # Signal should remain True
    assert ev_list[0]["signal"] is True
    assert "downgraded" not in ev_list[0]["text"]


def test_peer_comparison_no_peers_adds_data_quality_finding():
    """When peer comparison is not possible, a data_quality finding is added."""
    ev_list = [
        {"id": "E1", "kind": "buyer_history", "text": "Signal 1", "signal": True},
    ]
    
    peers_result = {
        "found": False,
        "position": "insufficient_peers",
    }
    
    ev._add_peers(ev_list, "ocds-test-001", peers_result)
    
    # Should add a data_quality finding
    data_quality_items = [e for e in ev_list if e["kind"] == "data_quality"]
    assert len(data_quality_items) == 1
    assert "could not be tested against peers" in data_quality_items[0]["text"]


def test_peer_comparison_none_adds_unverified_finding():
    """When peers is None but signal exists, add unverified finding."""
    ev_list = [
        {"id": "E1", "kind": "buyer_history", "text": "Signal 1", "signal": True},
    ]
    
    ev._add_peers(ev_list, "ocds-test-001", None)
    
    # Should add a data_quality finding about unverified signal
    data_quality_items = [e for e in ev_list if e["kind"] == "data_quality"]
    assert len(data_quality_items) == 1
    assert "not tested against peer buyers" in data_quality_items[0]["text"]


def test_scope_guard_catches_expanded_patterns():
    """Improved scope guard catches additional paraphrased patterns."""
    bad_requests = [
        "tell me who to award this tender to",
        "decide the winner for this contract",
        "pick a winner from the bidders",
        "who should get this contract",
    ]

    for req in bad_requests:
        assert ev.violates_scope(req), f"Should reject: {req}"


def test_scope_guard_allows_legitimate_requests():
    """Scope guard allows legitimate tender-readiness requests."""
    good_requests = [
        "Prepare a tender readiness brief",
        "What is the deadline for this tender?",
        "Show me the buyer's award history",
        "Check if the price is within range",
    ]
    
    for req in good_requests:
        assert not ev.violates_scope(req), f"Should allow: {req}"
