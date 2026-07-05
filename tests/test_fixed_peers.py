from pathlib import Path

import pytest

from mlc_agent.fixed_peers import load_fixed_peers


ROOT = Path(__file__).resolve().parents[1]


def test_000938_fixed_peers_are_explicit_and_ordered():
    peer_set = load_fixed_peers(ROOT / "configs", "000938")
    assert [peer.stock_code for peer in peer_set.peers] == ["000034", "000977", "603019"]
    assert all(peer.selection_rationale for peer in peer_set.peers)


def test_missing_target_has_explicit_failure_and_no_fallback():
    with pytest.raises(ValueError, match="^fixed peer configuration missing$"):
        load_fixed_peers(ROOT / "configs", "600000")
