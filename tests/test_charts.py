from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from mlc_agent.charts import (
    CHART_FIELDS,
    build_stock_charts,
    normalize_series_to_100,
    peer_legend_labels,
    prepare_peer_chart,
    stock_chart_node,
    validate_chart_history,
)
from mlc_agent.config import load_template_mapping
from mlc_agent.docx_writer import validate_template_mapping
from mlc_agent.docx_writer import write_docx_by_mapping
from mlc_agent.market_history import (
    AlignedMarketHistory,
    DrawdownResult,
    MarketHistoryResult,
    MarketInstrument,
    MarketPerformanceAnalysis,
    MarketPoint,
    MarketSeries,
    MarketSnapshot,
)
from mlc_agent.schemas import FieldResult


DATES = [date(2024, 7, 3), date(2025, 7, 3), date(2026, 7, 3)]


def series(code: str, values: list[int], kind: str, *, dates=DATES) -> MarketSeries:
    return MarketSeries(
        instrument=MarketInstrument(
            stock_code=code,
            display_name=f"Company {code}",
            eastmoney_secid=f"0.{code}",
            kind=kind,
        ),
        adjustment="forward_adjusted",
        points=[
            MarketPoint(trading_date=day, close=Decimal(value))
            for day, value in zip(dates, values, strict=True)
        ],
        source_url=f"https://push2his.eastmoney.com/{code}?fqt=1",
    )


def market_result() -> MarketHistoryResult:
    target = series("000938", [20, 25, 30], "target")
    benchmark = series("399001", [100, 105, 110], "benchmark")
    peers = [
        series("600001", [50, 55, 60], "peer"),
        series("600002", [80, 76, 84], "peer"),
        series("600003", [25, 30, 35], "peer"),
    ]
    history = AlignedMarketHistory(
        start_date=DATES[0], end_date=DATES[-1], target=target, benchmark=benchmark, peers=peers
    )
    return MarketHistoryResult(
        aligned_history=history,
        snapshot=MarketSnapshot(
            quote_date=DATES[-1], current_price=Decimal("30"), week_52_high=Decimal("31"),
            week_52_low=Decimal("19"), source_url=target.source_url,
        ),
        analysis=MarketPerformanceAnalysis(
            target_return=Decimal("0.5"), benchmark_return=Decimal("0.1"),
            peer_returns=[Decimal("0.2"), Decimal("0.05"), Decimal("0.4")],
            peer_median_return=Decimal("0.2"), aligns_to_index=False, aligns_to_peers=False,
            maximum_drawdown=DrawdownResult(
                maximum_drawdown=Decimal("0"), peak_date=DATES[-1], trough_date=DATES[-1],
                peak_close=Decimal("30"), trough_close=Decimal("30"),
            ),
            significant_drop=False, start_date=DATES[0], end_date=DATES[-1],
        ),
    )


def test_normalization_sets_first_common_date_to_100_without_rounding():
    normalized = normalize_series_to_100(market_result().aligned_history.target)
    assert normalized.dates == DATES
    assert normalized.values == [Decimal("100"), Decimal("125.00"), Decimal("150.0")]


def test_peer_chart_uses_target_then_exactly_three_peers_and_excludes_benchmark():
    normalized = prepare_peer_chart(market_result().aligned_history)
    assert [item.stock_code for item in normalized] == ["000938", "600001", "600002", "600003"]
    assert "399001" not in {item.stock_code for item in normalized}
    assert all(item.values[0] == Decimal("100") for item in normalized)
    assert peer_legend_labels(normalized) == [
        "Company 000938 (000938)",
        "Company 600001 (600001)",
        "Company 600002 (600002)",
        "Company 600003 (600003)",
    ]


def test_missing_peer_or_misaligned_dates_fails_both_chart_fields(tmp_path):
    result = market_result()
    missing_peer_history = result.aligned_history.model_copy(update={
        "peers": result.aligned_history.peers[:2]
    })
    with pytest.raises(ValueError, match="exactly three peers"):
        validate_chart_history(missing_peer_history)

    bad_peer = result.aligned_history.peers[0].model_copy(update={
        "points": result.aligned_history.peers[0].points[:-1]
    })
    bad_history = result.aligned_history.model_copy(update={
        "peers": [bad_peer, *result.aligned_history.peers[1:]]
    })
    bad_result = result.model_copy(update={"aligned_history": bad_history})
    charts = build_stock_charts(
        bad_result, run_dir=tmp_path, generated_at=datetime(2026, 7, 3, tzinfo=timezone.utc)
    )
    assert not charts.artifacts
    assert [item.field_id for item in charts.errors] == list(CHART_FIELDS)


def test_real_render_creates_two_white_1600_by_750_pngs_with_evidence(tmp_path):
    result = build_stock_charts(
        market_result(), run_dir=tmp_path, generated_at=datetime(2026, 7, 3, tzinfo=timezone.utc)
    )
    assert not result.errors
    assert [item.field_id for item in result.artifacts] == list(CHART_FIELDS)
    assert len(result.source_values) == 2
    for artifact in result.artifacts:
        with Image.open(artifact.path) as image:
            assert image.size == (1600, 750)
            assert image.convert("RGB").getpixel((0, 0)) == (255, 255, 255)
    standalone, peer = result.source_values
    assert standalone.metadata["adjustment"] == peer.metadata["adjustment"] == "forward_adjusted"
    assert standalone.metadata["data_cutoff"] == peer.metadata["data_cutoff"] == "2026-07-03"
    assert len(standalone.item_evidence) == 1
    assert [item.item_id for item in peer.item_evidence] == ["000938", "600001", "600002", "600003"]


def test_node_consumes_only_part08_market_history_contract(tmp_path):
    source = market_result()
    state = {
        "part_results": {"part_08": {"market_history": source.model_dump(mode="json")}},
        "source_values": [], "artifacts": [], "node_errors": [],
        "run_dir": str(tmp_path), "created_at": "2026-07-03T09:00:00+00:00",
    }
    update = stock_chart_node(state)
    assert update["part_results"]["part_09"]["normalized_peer_series"]
    assert not update["node_errors"]
    assert len(update["artifacts"]) == 2


@pytest.mark.parametrize("part_results", [{}, {"part_08": {"status": "failed"}}])
def test_node_part08_missing_or_failed_returns_two_errors_without_images(tmp_path, part_results):
    update = stock_chart_node({
        "part_results": part_results,
        "source_values": [], "artifacts": [], "node_errors": [],
        "run_dir": str(tmp_path), "created_at": "2026-07-03T09:00:00+00:00",
    })
    assert not update["source_values"] and not update["artifacts"]
    assert [item["field_id"] for item in update["node_errors"]] == list(CHART_FIELDS)
    assert all("missing or Part 08 failed" in item["message"] for item in update["node_errors"])


def test_part09_mapping_matches_real_template():
    root = Path(__file__).resolve().parents[1]
    mapping = load_template_mapping(root / "configs")
    fields = [item for item in mapping["fields"] if item["field_id"] in CHART_FIELDS]
    assert len(fields) == 2
    assert all(item["write_strategy"] == "insert_image" for item in fields)
    validate_template_mapping(root / "Workup_template_260617-外测版.docx", fields)


def test_common_writer_embeds_two_images_and_preserves_aspect_ratio(tmp_path):
    root = Path(__file__).resolve().parents[1]
    chart_result = build_stock_charts(
        market_result(), run_dir=tmp_path, generated_at=datetime(2026, 7, 3, tzinfo=timezone.utc)
    )
    mapping = load_template_mapping(root / "configs")
    fields = [item for item in mapping["fields"] if item["field_id"] in CHART_FIELDS]
    artifacts = {item.field_id: item for item in chart_result.artifacts}
    results = {
        field_id: FieldResult(
            field_id=field_id,
            label=field_id,
            value=Path(artifacts[field_id].path).name,
            selected_source="market_history",
            artifact_path=artifacts[field_id].path,
        )
        for field_id in CHART_FIELDS
    }
    output = tmp_path / "charts_result.docx"
    assert write_docx_by_mapping(
        root / "Workup_template_260617-外测版.docx", output, fields, results
    ) == []
    written = Document(output)
    image_relationships = [
        rel for rel in written.part.rels.values() if rel.reltype == RT.IMAGE
    ]
    assert len(image_relationships) == 2
    with ZipFile(root / "Workup_template_260617-外测版.docx") as archive:
        template_media_count = len([
            name for name in archive.namelist() if name.startswith("word/media/")
        ])
    with ZipFile(output) as archive:
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
    assert len(media) - template_media_count == 2
    assert len(written.inline_shapes) == 2
    expected_ratio = 1600 / 750
    for shape in written.inline_shapes:
        assert shape.width.inches == pytest.approx(6.3, abs=0.01)
        assert shape.width / shape.height == pytest.approx(expected_ratio, rel=0.01)
