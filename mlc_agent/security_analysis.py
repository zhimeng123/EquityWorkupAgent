from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from mlc_agent.market_history import (
    MarketHistoryResult,
    market_history,
    subtract_calendar_months,
)
from mlc_agent.schemas import (
    CompanyIdentity,
    DerivedEvidence,
    DerivedInput,
    ItemEvidence,
    SourceValue,
    WorkupAgentState,
)


DocumentSource = Literal["annual_report", "interim_report", "cninfo", "exchange", "eastmoney"]
IpoSource = Literal["annual_report", "interim_report", "exchange", "eastmoney"]


class IpoEvidence(BaseModel):
    ipo_date: date
    source: IpoSource
    source_url: str
    evidence_period: str = Field(min_length=1)


class SecuritiesOffering(BaseModel):
    offering_id: str = Field(min_length=1)
    offering_type: str = Field(min_length=1)
    announcement_date: date
    size_cny: Decimal | None = Field(default=None, gt=0)
    price_cny: Decimal | None = Field(default=None, gt=0)
    status: str = Field(min_length=1)
    source: Literal["annual_report", "interim_report", "cninfo", "exchange"]
    source_url: str


class SecuritiesOfferingReview(BaseModel):
    window_start: date
    window_end: date
    catalog_source: Literal["cninfo", "exchange"]
    catalog_source_url: str
    offerings: list[SecuritiesOffering] = Field(default_factory=list)


class SecurityAnalysisResult(BaseModel):
    status: Literal["completed", "partial", "failed"]
    subresults: dict[str, Literal["completed", "partial", "failed"]] = Field(default_factory=dict)
    market_history: MarketHistoryResult | None = None
    source_values: list[SourceValue]
    included_offerings: list[SecuritiesOffering]
    errors: list[str] = Field(default_factory=list)


def select_offerings_in_window(
    offerings: list[SecuritiesOffering],
    *,
    as_of: date,
) -> list[SecuritiesOffering]:
    start = subtract_calendar_months(as_of, 12)
    return sorted(
        (item for item in offerings if start <= item.announcement_date <= as_of),
        key=lambda item: (item.announcement_date, item.offering_id),
    )


def select_ipo_evidence(candidates: list[IpoEvidence]) -> IpoEvidence:
    formal = [item for item in candidates if item.source != "eastmoney"]
    selected_pool = formal or [item for item in candidates if item.source == "eastmoney"]
    if not selected_pool:
        raise ValueError("no verified formal-report, exchange, or structured candidate evidence")
    dates = {item.ipo_date for item in selected_pool}
    if len(dates) != 1:
        raise ValueError("IPO date candidates conflict within the selected evidence tier")
    priority = {"exchange": 0, "annual_report": 1, "interim_report": 2, "eastmoney": 3}
    return sorted(selected_pool, key=lambda item: (priority[item.source], item.source_url))[0]


def _market_input(
    field_id: str,
    value: Any,
    period: str,
    source_url: str,
) -> DerivedInput:
    return DerivedInput(
        field_id=field_id,
        value=value,
        period=period,
        source_url=source_url,
    )


def _format_offerings(items: list[SecuritiesOffering]) -> str:
    if not items:
        return "No. No securities offering was identified in the 12-month window."
    lines = ["Yes."]
    for item in items:
        size = (
            f"CNY {item.size_cny / Decimal('1000000'):,.2f} million"
            if item.size_cny is not None
            else "Not disclosed"
        )
        price = f"CNY {item.price_cny:,.2f}" if item.price_cny is not None else "Not disclosed"
        lines.append(
            f"{item.announcement_date.isoformat()} - {item.offering_type}; size: {size}; "
            f"price: {price}; status: {item.status}."
        )
    return "\n".join(lines)


def _collect_security_analysis_complete(
    *,
    market: MarketHistoryResult,
    ipo_candidates: list[IpoEvidence],
    offering_review: SecuritiesOfferingReview | None,
    as_of: date,
    captured_at: datetime,
) -> SecurityAnalysisResult:
    values: list[SourceValue] = []
    errors: list[str] = []
    analysis = market.analysis
    history = market.aligned_history
    period = f"{analysis.start_date.isoformat()} to {analysis.end_date.isoformat()}"
    try:
        ipo_evidence = select_ipo_evidence(ipo_candidates)
        values.append(
            SourceValue(
                field_id="ipo_date",
                value=ipo_evidence.ipo_date.isoformat(),
                raw_value=ipo_evidence.model_dump(mode="json"),
                source=ipo_evidence.source,
                source_url=ipo_evidence.source_url,
                captured_at=captured_at,
                period=ipo_evidence.evidence_period,
            )
        )
    except ValueError as exc:
        errors.append(f"ipo_date: {exc}")
    return_inputs = [
        _market_input("target_24m_return", analysis.target_return, period, history.target.source_url),
        _market_input("benchmark_24m_return", analysis.benchmark_return, period, history.benchmark.source_url),
    ]
    values.append(
        SourceValue(
            field_id="align_to_index",
            value=(
                f"{'Yes' if analysis.aligns_to_index else 'No'}. The 24-month forward-adjusted return was "
                f"{analysis.target_return:.2%} versus {analysis.benchmark_return:.2%} for "
                f"{history.benchmark.instrument.display_name}; absolute difference "
                f"{abs(analysis.target_return - analysis.benchmark_return):.2%}."
            ),
            raw_value=analysis.model_dump(mode="json"),
            source="derived",
            source_url=history.target.source_url,
            captured_at=captured_at,
            period=period,
            metadata={"adjustment": "forward_adjusted"},
            derived_evidence=DerivedEvidence(
                formula="align to index = abs(target cumulative return - benchmark cumulative return) <= 15 percentage points",
                inputs=return_inputs,
                result=analysis.aligns_to_index,
            ),
        )
    )
    peer_inputs = [
        _market_input("target_24m_return", analysis.target_return, period, history.target.source_url),
        *[
            _market_input(
                f"peer_return:{series.instrument.stock_code}",
                peer_return,
                period,
                series.source_url,
            )
            for series, peer_return in zip(history.peers, analysis.peer_returns, strict=True)
        ],
    ]
    values.append(
        SourceValue(
            field_id="align_to_peers",
            value=(
                f"{'Yes' if analysis.aligns_to_peers else 'No'}. The 24-month forward-adjusted return was "
                f"{analysis.target_return:.2%} versus a three-peer median of "
                f"{analysis.peer_median_return:.2%}; absolute difference "
                f"{abs(analysis.target_return - analysis.peer_median_return):.2%}."
            ),
            raw_value=analysis.model_dump(mode="json"),
            source="derived",
            source_url=history.target.source_url,
            captured_at=captured_at,
            period=period,
            metadata={"adjustment": "forward_adjusted"},
            derived_evidence=DerivedEvidence(
                formula="peer median = median(three peer cumulative returns); align to peers = abs(target cumulative return - peer median) <= 15 percentage points",
                inputs=peer_inputs,
                result=analysis.aligns_to_peers,
            ),
        )
    )
    drawdown = analysis.maximum_drawdown
    drawdown_inputs = [
        _market_input("drawdown_peak_close", drawdown.peak_close, drawdown.peak_date.isoformat(), history.target.source_url),
        _market_input("drawdown_trough_close", drawdown.trough_close, drawdown.trough_date.isoformat(), history.target.source_url),
    ]
    values.extend(
        [
            SourceValue(
                field_id="significant_stock_drop",
                value="Yes" if analysis.significant_drop else "No",
                raw_value=drawdown.model_dump(mode="json"),
                source="derived",
                source_url=history.target.source_url,
                captured_at=captured_at,
                period=period,
                metadata={"adjustment": "forward_adjusted"},
                derived_evidence=DerivedEvidence(
                    formula="maximum drawdown = max((prior peak close - later trough close) / prior peak close); significant = maximum drawdown >= 30%",
                    inputs=drawdown_inputs,
                    result=analysis.significant_drop,
                ),
            ),
            SourceValue(
                field_id="significant_stock_drop_details",
                value=(
                    f" Maximum drawdown was {drawdown.maximum_drawdown:.2%}, from a peak of "
                    f"CNY {drawdown.peak_close:.2f} on {drawdown.peak_date.isoformat()} to "
                    f"CNY {drawdown.trough_close:.2f} on {drawdown.trough_date.isoformat()}, "
                    "using forward-adjusted daily closes."
                ),
                raw_value=drawdown.model_dump(mode="json"),
                source="derived",
                source_url=history.target.source_url,
                captured_at=captured_at,
                period=period,
                metadata={"adjustment": "forward_adjusted"},
                derived_evidence=DerivedEvidence(
                    formula="maximum drawdown = (peak close - subsequent trough close) / peak close",
                    inputs=drawdown_inputs,
                    result=drawdown.model_dump(mode="json"),
                ),
            ),
        ]
    )
    snapshot = market.snapshot
    snapshot_period = snapshot.quote_date.isoformat()
    for field_id, value, metric in (
        ("current_price", snapshot.current_price, "unadjusted latest close"),
        ("week_52_high", snapshot.week_52_high, "unadjusted trailing-365-day daily high"),
        ("week_52_low", snapshot.week_52_low, "unadjusted trailing-365-day daily low"),
    ):
        values.append(
            SourceValue(
                field_id=field_id,
                value=f"CNY {value:.2f}",
                raw_value=value,
                source="market_history",
                source_url=snapshot.source_url,
                captured_at=captured_at,
                period=snapshot_period,
                metadata={"adjustment": "unadjusted", "metric": metric},
            )
        )
    window_start = subtract_calendar_months(as_of, 12)
    offerings_period = f"{window_start.isoformat()} to {as_of.isoformat()}"
    included_offerings: list[SecuritiesOffering] = []
    if offering_review is None:
        errors.append("securities_offerings: no verified complete announcement-window review")
    elif offering_review.window_start != window_start or offering_review.window_end != as_of:
        errors.append("securities_offerings: announcement review window does not match the required 12 months")
    else:
        included_offerings = select_offerings_in_window(offering_review.offerings, as_of=as_of)
        offering_evidence = [
            ItemEvidence(
                item_id=item.offering_id,
                source=item.source,
                source_url=item.source_url,
                period=item.announcement_date.isoformat(),
                raw_value=item.model_dump(mode="json"),
            )
            for item in included_offerings
        ]
        values.append(
            SourceValue(
                field_id="securities_offerings",
                value=_format_offerings(included_offerings),
                raw_value=[item.model_dump(mode="json") for item in included_offerings],
                source=offering_review.catalog_source,
                source_url=offering_review.catalog_source_url,
                captured_at=captured_at,
                period=offerings_period,
                item_evidence=offering_evidence,
                metadata={"window_complete": True},
            )
        )
    return SecurityAnalysisResult(
        status="completed" if not errors else "partial",
        subresults={
            "ipo_offerings": "completed" if not any(
                error.startswith(("ipo_date:", "securities_offerings:")) for error in errors
            ) else "partial",
            "market_history": "completed",
            "security_calculations": "completed",
        },
        market_history=market,
        source_values=values,
        included_offerings=included_offerings,
        errors=errors,
    )


def _collect_extraction_only(
    *,
    ipo_candidates: list[IpoEvidence],
    offering_review: SecuritiesOfferingReview | None,
    as_of: date,
    captured_at: datetime,
) -> tuple[list[SourceValue], list[SecuritiesOffering], list[str]]:
    values: list[SourceValue] = []
    errors: list[str] = []
    included_offerings: list[SecuritiesOffering] = []
    try:
        ipo_evidence = select_ipo_evidence(ipo_candidates)
        values.append(SourceValue(
            field_id="ipo_date",
            value=ipo_evidence.ipo_date.isoformat(),
            raw_value=ipo_evidence.model_dump(mode="json"),
            source=ipo_evidence.source,
            source_url=ipo_evidence.source_url,
            captured_at=captured_at,
            period=ipo_evidence.evidence_period,
        ))
    except ValueError as exc:
        errors.append(f"ipo_offerings: ipo_date: {exc}")

    window_start = subtract_calendar_months(as_of, 12)
    if offering_review is None:
        errors.append("ipo_offerings: securities_offerings: no verified complete announcement-window review")
    elif offering_review.window_start != window_start or offering_review.window_end != as_of:
        errors.append("ipo_offerings: securities_offerings: announcement review window does not match the required 12 months")
    else:
        included_offerings = select_offerings_in_window(offering_review.offerings, as_of=as_of)
        values.append(SourceValue(
            field_id="securities_offerings",
            value=_format_offerings(included_offerings),
            raw_value=[item.model_dump(mode="json") for item in included_offerings],
            source=offering_review.catalog_source,
            source_url=offering_review.catalog_source_url,
            captured_at=captured_at,
            period=f"{window_start.isoformat()} to {as_of.isoformat()}",
            item_evidence=[ItemEvidence(
                item_id=item.offering_id,
                source=item.source,
                source_url=item.source_url,
                period=item.announcement_date.isoformat(),
                raw_value=item.model_dump(mode="json"),
            ) for item in included_offerings],
            metadata={"window_complete": True},
        ))
    return values, included_offerings, errors


def collect_security_analysis(
    *,
    market: MarketHistoryResult | None,
    ipo_candidates: list[IpoEvidence],
    offering_review: SecuritiesOfferingReview | None,
    as_of: date,
    captured_at: datetime,
    extraction_error: str | None = None,
    market_error: str | None = None,
) -> SecurityAnalysisResult:
    """Collect independent Part 08 subresults without losing successful fields."""
    if market is None:
        values, included_offerings, errors = _collect_extraction_only(
            ipo_candidates=ipo_candidates,
            offering_review=offering_review,
            as_of=as_of,
            captured_at=captured_at,
        )
        if extraction_error:
            errors = [extraction_error]
        errors.append(market_error or "market_history: no verified market history result")
        return SecurityAnalysisResult(
            status="failed" if not values else "partial",
            subresults={
                "ipo_offerings": "failed" if extraction_error else ("completed" if not errors[:-1] else "partial"),
                "market_history": "failed",
                "security_calculations": "failed",
            },
            market_history=None,
            source_values=values,
            included_offerings=included_offerings,
            errors=errors,
        )

    result = _collect_security_analysis_complete(
        market=market,
        ipo_candidates=ipo_candidates,
        offering_review=offering_review,
        as_of=as_of,
        captured_at=captured_at,
    )
    if extraction_error:
        result.source_values = [
            item for item in result.source_values
            if item.field_id not in {"ipo_date", "securities_offerings"}
        ]
        result.included_offerings = []
        result.errors = [
            error for error in result.errors
            if not error.startswith(("ipo_date:", "securities_offerings:"))
        ]
        result.errors.append(extraction_error)
        result.subresults["ipo_offerings"] = "failed"
        result.status = "partial"
    return result


def security_analysis_node(
    state: WorkupAgentState,
    *,
    client: httpx.Client,
) -> dict[str, Any]:
    inputs = state.get("part_results", {}).get("part_08_input")
    if not isinstance(inputs, dict):
        raise ValueError("part_results.part_08_input is required")
    part_05 = state.get("part_results", {}).get("part_05")
    if not isinstance(part_05, dict):
        raise ValueError("part_results.part_05 selected peers are required")
    selected_codes = [
        item["stock_code"]
        for item in part_05.get("selection", {}).get("selected", [])
    ]
    peer_identities = [
        CompanyIdentity.model_validate(item) for item in inputs.get("peer_identities", [])
    ]
    if [item.stock_code for item in peer_identities] != selected_codes:
        raise ValueError("part_08 peer identities must match part_05 selected peers in order")
    company = CompanyIdentity.model_validate(state["company"])
    as_of = date.fromisoformat(inputs["as_of"])
    try:
        history = market_history(
            client,
            target=company,
            peers=peer_identities,
            as_of=as_of,
        )
    except (httpx.HTTPError, ValueError) as exc:
        reason = f"market_history: {exc}"
        ipo_candidates = [IpoEvidence.model_validate(item) for item in inputs.get("ipo_candidates", [])]
        offering_review = (
            SecuritiesOfferingReview.model_validate(inputs["offering_review"])
            if inputs.get("offering_review")
            else None
        )
        if ipo_candidates or offering_review is not None:
            result = collect_security_analysis(
                market=None,
                ipo_candidates=ipo_candidates,
                offering_review=offering_review,
                as_of=as_of,
                captured_at=datetime.fromisoformat(state["created_at"]),
                market_error=reason,
            )
            part_results = dict(state.get("part_results", {}))
            part_results["part_08"] = result.model_dump(mode="json")
            return {
                "part_results": part_results,
                "source_values": list(state.get("source_values", []))
                + [item.model_dump(mode="json") for item in result.source_values],
                "node_errors": list(state.get("node_errors", []))
                + [{"node": "part_08", "message": item} for item in result.errors],
            }
        part_results = dict(state.get("part_results", {}))
        part_results["part_08"] = {"status": "failed", "reason": reason}
        return {
            "part_results": part_results,
            "source_values": list(state.get("source_values", [])),
            "node_errors": list(state.get("node_errors", []))
            + [{"node": "part_08", "message": reason}],
        }
    result = collect_security_analysis(
        market=history,
        ipo_candidates=[IpoEvidence.model_validate(item) for item in inputs.get("ipo_candidates", [])],
        offering_review=(
            SecuritiesOfferingReview.model_validate(inputs["offering_review"])
            if inputs.get("offering_review")
            else None
        ),
        as_of=as_of,
        captured_at=datetime.fromisoformat(state["created_at"]),
    )
    part_results = dict(state.get("part_results", {}))
    part_results["part_08"] = result.model_dump(mode="json")
    return {
        "part_results": part_results,
        "source_values": list(state.get("source_values", [])) + [item.model_dump(mode="json") for item in result.source_values],
        "node_errors": list(state.get("node_errors", [])) + [{"node": "part_08", "message": item} for item in result.errors],
    }
