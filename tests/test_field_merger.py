from datetime import datetime, timezone

from mlc_agent.field_merger import merge_field_values
from mlc_agent.schemas import SourceValue


def candidate(source: str, value: str) -> SourceValue:
    return SourceValue(
        field_id="current_price",
        value=value,
        raw_value=value,
        source=source,
        source_url=f"https://example.com/{source}",
        captured_at=datetime.now(timezone.utc),
    )


def test_market_priority_prefers_verified_history_and_records_conflict():
    mappings = [{"field_id": "current_price", "label": "Current Price"}]
    priorities = {"current_price": ["market_history", "xueqiu", "eastmoney"]}
    results, evidence, conflicts, failures = merge_field_values(
        mappings,
        priorities,
        [
            candidate("eastmoney", "CNY 10.01"),
            candidate("xueqiu", "CNY 10.02"),
            candidate("market_history", "CNY 10.03"),
        ],
    )
    assert failures == []
    assert results["current_price"].selected_source == "market_history"
    assert {item["source"] for item in evidence[0].conflict_values} == {"xueqiu", "eastmoney"}
    assert len(conflicts) == 1


def test_missing_field_is_not_fabricated():
    mappings = [{"field_id": "current_price", "label": "Current Price"}]
    priorities = {"current_price": ["xueqiu", "eastmoney"]}
    results, evidence, conflicts, failures = merge_field_values(mappings, priorities, [])
    assert results == {}
    assert evidence == []
    assert conflicts == []
    assert failures[0].field_id == "current_price"


def test_merger_transfers_structured_and_artifact_metadata_and_rejects_unconfigured_source():
    mapping = [{"field_id": "current_price", "label": "Current Price"}]
    priorities = {"current_price": ["market_history"]}
    configured = candidate("market_history", "chart")
    configured.metadata = {
        "structured_value": [["A", "B"]],
        "artifact_path": "/tmp/chart.png",
    }
    unconfigured = candidate("eastmoney", "wrong")
    results, _, _, failures = merge_field_values(
        mapping, priorities, [unconfigured, configured]
    )
    assert failures == []
    assert results["current_price"].selected_source == "market_history"
    assert results["current_price"].structured_value == [["A", "B"]]
    assert results["current_price"].artifact_path == "/tmp/chart.png"

    results, _, _, failures = merge_field_values(mapping, priorities, [unconfigured])
    assert results == {}
    assert failures[0].source_attempted == ["market_history"]
