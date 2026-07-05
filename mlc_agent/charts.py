from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from mlc_agent.market_history import AlignedMarketHistory, MarketHistoryResult, MarketSeries
from mlc_agent.schemas import ItemEvidence, SourceValue


CHART_WIDTH_PX = 1600
CHART_HEIGHT_PX = 750
CHART_DPI = 100
CHART_FIELDS = ("standalone_stock_chart", "peer_comparison_chart")


class NormalizedSeries(BaseModel):
    stock_code: str
    display_name: str
    dates: list[date] = Field(min_length=2)
    values: list[Decimal] = Field(min_length=2)
    source_url: str


class ChartArtifact(BaseModel):
    field_id: Literal["standalone_stock_chart", "peer_comparison_chart"]
    artifact_type: Literal["png"] = "png"
    path: str
    width_px: Literal[1600] = CHART_WIDTH_PX
    height_px: Literal[750] = CHART_HEIGHT_PX


class ChartError(BaseModel):
    field_id: str
    reason: str


class StockChartResult(BaseModel):
    source_values: list[SourceValue] = Field(default_factory=list)
    artifacts: list[ChartArtifact] = Field(default_factory=list)
    normalized_peer_series: list[NormalizedSeries] = Field(default_factory=list)
    errors: list[ChartError] = Field(default_factory=list)


def _label(series: MarketSeries) -> str:
    return f"{series.instrument.display_name} ({series.instrument.stock_code})"


def validate_chart_history(history: AlignedMarketHistory) -> list[date]:
    if len(history.peers) != 3:
        raise ValueError("peer comparison chart requires exactly three peers")
    series = [history.target, history.benchmark, *history.peers]
    if any(item.adjustment != "forward_adjusted" for item in series):
        raise ValueError("charts require forward-adjusted history")
    expected_dates = [item.trading_date for item in history.target.points]
    if len(expected_dates) < 2:
        raise ValueError("charts require at least two common trading dates")
    if expected_dates[0] != history.start_date or expected_dates[-1] != history.end_date:
        raise ValueError("aligned history range does not match target points")
    for item in [history.benchmark, *history.peers]:
        if [point.trading_date for point in item.points] != expected_dates:
            raise ValueError(f"aligned history dates differ for {item.instrument.stock_code}")
    return expected_dates


def normalize_series_to_100(series: MarketSeries) -> NormalizedSeries:
    if series.adjustment != "forward_adjusted":
        raise ValueError("normalization requires forward-adjusted history")
    if len(series.points) < 2:
        raise ValueError(f"{series.instrument.stock_code}: insufficient chart history")
    first = series.points[0].close
    if first <= 0:
        raise ValueError(f"{series.instrument.stock_code}: first close must be positive")
    return NormalizedSeries(
        stock_code=series.instrument.stock_code,
        display_name=series.instrument.display_name,
        dates=[item.trading_date for item in series.points],
        values=[item.close / first * Decimal("100") for item in series.points],
        source_url=series.source_url,
    )


def prepare_peer_chart(history: AlignedMarketHistory) -> list[NormalizedSeries]:
    validate_chart_history(history)
    return [normalize_series_to_100(item) for item in [history.target, *history.peers]]


def peer_legend_labels(normalized: list[NormalizedSeries]) -> list[str]:
    return [f"{item.display_name} ({item.stock_code})" for item in normalized]


def _load_pyplot() -> Any:
    try:
        import matplotlib
    except ImportError as exc:
        raise RuntimeError(
            "Chart rendering requires the approved matplotlib==3.11.0 dependency"
        ) from exc
    if matplotlib.__version__ != "3.11.0":
        raise RuntimeError(
            f"Chart rendering requires matplotlib==3.11.0, found {matplotlib.__version__}"
        )
    matplotlib.use("Agg")
    from matplotlib import pyplot

    return pyplot


def _style_axis(axis: Any) -> None:
    axis.grid(axis="y", color="#D9E2F3", linewidth=0.8, alpha=0.8)
    axis.set_facecolor("white")
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.tick_params(colors="#404040", labelsize=10)


def render_standalone_chart(history: AlignedMarketHistory, output_path: Path) -> Path:
    dates = validate_chart_history(history)
    pyplot = _load_pyplot()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = pyplot.subplots(figsize=(16, 7.5), dpi=CHART_DPI, facecolor="white")
    target = history.target
    axis.plot(
        dates,
        [float(item.close) for item in target.points],
        color="#1F4E78",
        linewidth=2.2,
        label=_label(target),
    )
    _style_axis(axis)
    axis.set_title(f"Forward-adjusted Closing Price - {_label(target)}", fontsize=17, loc="left")
    axis.set_ylabel("Forward-adjusted close", fontsize=11)
    axis.legend(loc="upper left", frameon=False)
    figure.text(
        0.01,
        0.01,
        f"Range: {history.start_date.isoformat()} to {history.end_date.isoformat()} | "
        f"Forward-adjusted daily close | Data cutoff: {history.end_date.isoformat()}",
        fontsize=9,
        color="#666666",
    )
    figure.autofmt_xdate(rotation=0)
    figure.subplots_adjust(left=0.07, right=0.98, top=0.90, bottom=0.13)
    figure.savefig(output_path, dpi=CHART_DPI, facecolor="white")
    pyplot.close(figure)
    return output_path


def render_peer_comparison_chart(
    normalized: list[NormalizedSeries],
    *,
    start_date: date,
    end_date: date,
    output_path: Path,
) -> Path:
    if len(normalized) != 4:
        raise ValueError("peer chart requires target plus exactly three peers")
    expected_dates = normalized[0].dates
    if any(item.dates != expected_dates for item in normalized):
        raise ValueError("peer chart series do not use the same common dates")
    if any(item.values[0] != Decimal("100") for item in normalized):
        raise ValueError("every peer comparison series must start at 100")
    pyplot = _load_pyplot()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    colors = ("#1F4E78", "#C55A11", "#548235", "#7030A0")
    figure, axis = pyplot.subplots(figsize=(16, 7.5), dpi=CHART_DPI, facecolor="white")
    legend_labels = peer_legend_labels(normalized)
    for item, color, legend_label in zip(normalized, colors, legend_labels, strict=True):
        axis.plot(
            item.dates,
            [float(value) for value in item.values],
            linewidth=2.2,
            color=color,
            label=legend_label,
        )
    _style_axis(axis)
    axis.axhline(100, color="#808080", linewidth=0.8, linestyle="--")
    axis.set_title("Forward-adjusted Peer Performance (First Common Trading Day = 100)", fontsize=17, loc="left")
    axis.set_ylabel("Normalized performance", fontsize=11)
    axis.legend(loc="upper left", frameon=False, ncol=2)
    figure.text(
        0.01,
        0.01,
        f"Range: {start_date.isoformat()} to {end_date.isoformat()} | "
        f"Forward-adjusted daily close | Data cutoff: {end_date.isoformat()}",
        fontsize=9,
        color="#666666",
    )
    figure.autofmt_xdate(rotation=0)
    figure.subplots_adjust(left=0.07, right=0.98, top=0.90, bottom=0.13)
    figure.savefig(output_path, dpi=CHART_DPI, facecolor="white")
    pyplot.close(figure)
    return output_path


def build_stock_charts(
    market_result: MarketHistoryResult,
    *,
    run_dir: Path,
    generated_at: datetime,
) -> StockChartResult:
    history = market_result.aligned_history
    try:
        normalized = prepare_peer_chart(history)
    except Exception as exc:
        return StockChartResult(
            errors=[ChartError(field_id=field_id, reason=str(exc)) for field_id in CHART_FIELDS]
        )
    paths = {
        "standalone_stock_chart": run_dir / "standalone_stock_chart.png",
        "peer_comparison_chart": run_dir / "peer_comparison_chart.png",
    }
    artifacts: list[ChartArtifact] = []
    errors: list[ChartError] = []
    try:
        render_standalone_chart(history, paths["standalone_stock_chart"])
        artifacts.append(ChartArtifact(
            field_id="standalone_stock_chart", path=str(paths["standalone_stock_chart"].resolve())
        ))
    except Exception as exc:
        errors.append(ChartError(field_id="standalone_stock_chart", reason=str(exc)))
    try:
        render_peer_comparison_chart(
            normalized,
            start_date=history.start_date,
            end_date=history.end_date,
            output_path=paths["peer_comparison_chart"],
        )
        artifacts.append(ChartArtifact(
            field_id="peer_comparison_chart", path=str(paths["peer_comparison_chart"].resolve())
        ))
    except Exception as exc:
        errors.append(ChartError(field_id="peer_comparison_chart", reason=str(exc)))

    source_values = []
    artifact_by_field = {item.field_id: item for item in artifacts}
    series = [history.target, *history.peers]
    item_evidence = [
        ItemEvidence(
            item_id=item.instrument.stock_code,
            source="market_history",
            source_url=item.source_url,
            period=f"{history.start_date.isoformat()} to {history.end_date.isoformat()}",
            raw_value={
                "display_name": item.instrument.display_name,
                "adjustment": item.adjustment,
                "start_date": history.start_date,
                "end_date": history.end_date,
                "point_count": len(item.points),
            },
        )
        for item in series
    ]
    for field_id in CHART_FIELDS:
        artifact = artifact_by_field.get(field_id)
        if artifact is None:
            continue
        selected_evidence = item_evidence[:1] if field_id == "standalone_stock_chart" else item_evidence
        source_values.append(SourceValue(
            field_id=field_id,
            value=Path(artifact.path).name,
            raw_value={"artifact": artifact.model_dump(mode="json")},
            source="market_history",
            source_url=history.target.source_url,
            captured_at=generated_at,
            period=f"{history.start_date.isoformat()} to {history.end_date.isoformat()}",
            metadata={
                "artifact_path": artifact.path,
                "adjustment": "forward_adjusted",
                "data_cutoff": history.end_date.isoformat(),
                "width_px": artifact.width_px,
                "height_px": artifact.height_px,
            },
            item_evidence=selected_evidence,
        ))
    return StockChartResult(
        source_values=source_values,
        artifacts=artifacts,
        normalized_peer_series=normalized,
        errors=errors,
    )


def stock_chart_node(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("part_results", {}).get("part_08", {}).get("market_history")
    if raw is None:
        result = StockChartResult(errors=[
            ChartError(
                field_id=field_id,
                reason="part_results.part_08.market_history is missing or Part 08 failed",
            )
            for field_id in CHART_FIELDS
        ])
    else:
        try:
            market_result = MarketHistoryResult.model_validate(raw)
            result = build_stock_charts(
                market_result,
                run_dir=Path(state["run_dir"]),
                generated_at=datetime.fromisoformat(state["created_at"]),
            )
        except Exception as exc:
            result = StockChartResult(errors=[
                ChartError(field_id=field_id, reason=f"Invalid Part 08 market history: {exc}")
                for field_id in CHART_FIELDS
            ])
    return {
        "part_results": {**state.get("part_results", {}), "part_09": result.model_dump(mode="json")},
        "source_values": [*state.get("source_values", []), *(item.model_dump(mode="json") for item in result.source_values)],
        "artifacts": [*state.get("artifacts", []), *(item.model_dump(mode="json") for item in result.artifacts)],
        "node_errors": [*state.get("node_errors", []), *({"node": "part_09", "field_id": item.field_id, "message": item.reason} for item in result.errors)],
    }
