from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any

from pydantic import BaseModel, Field, model_validator

from mlc_agent.operating_performance import FinancialPeriodRecord
from mlc_agent.related_parties import (
    RelatedPartyTransaction,
    collect_related_party_transactions,
)
from mlc_agent.schemas import DerivedEvidence, DerivedInput, ItemEvidence, SourceValue, WorkupAgentState


TWO_PLACES = Decimal("0.01")
MILLION = Decimal("1000000")


class DisclosedMetric(BaseModel):
    value: Decimal = Field(ge=0)
    period: str = Field(min_length=1)
    source_url: str


class PeerCompany(BaseModel):
    stock_code: str = Field(pattern=r"^\d{6}$")
    company_name: str = Field(min_length=1)
    csrc_industry: str = Field(min_length=1)
    is_a_share: bool
    is_st: bool
    is_financial: bool
    annual_records: list[FinancialPeriodRecord]
    inventory_turnover: DisclosedMetric | None = None

    @model_validator(mode="after")
    def validate_annual_records(self) -> "PeerCompany":
        annual = [item for item in self.annual_records if item.period_type == "annual"]
        years = [item.fiscal_year for item in annual]
        if len(years) != len(set(years)):
            raise ValueError("duplicate annual financial period")
        return self


class PeerExclusion(BaseModel):
    stock_code: str
    reason: str


class PeerRanking(BaseModel):
    stock_code: str
    revenue_difference: Decimal


class PeerSelectionResult(BaseModel):
    success: bool
    comparison_year: int
    previous_year: int
    selected: list[PeerCompany] = Field(max_length=3)
    ranking: list[PeerRanking]
    exclusions: list[PeerExclusion]
    failure_reason: str | None = None


class CompanyComparisonMetrics(BaseModel):
    stock_code: str
    company_name: str
    comparison_year: int
    revenue_cny: Decimal
    gross_profit_cny: Decimal
    parent_net_profit_cny: Decimal
    revenue_growth: Decimal
    inventory_turnover: Decimal
    gross_margin: Decimal
    net_margin: Decimal
    revenue_source_url: str
    previous_revenue_source_url: str
    inventory_turnover_source_url: str


class PeerAnalysisResult(BaseModel):
    selection: PeerSelectionResult
    metrics: list[CompanyComparisonMetrics]
    source_values: list[SourceValue]
    failures: list[str] = Field(default_factory=list)


def _annual_by_year(company: PeerCompany) -> dict[int, FinancialPeriodRecord]:
    return {
        item.fiscal_year: item
        for item in company.annual_records
        if item.period_type == "annual"
    }


def select_peers(target: PeerCompany, candidates: list[PeerCompany]) -> PeerSelectionResult:
    target_annual = sorted(_annual_by_year(target), reverse=True)
    if len(target_annual) < 2 or target_annual[0] - target_annual[1] != 1:
        raise ValueError("target requires two consecutive complete annual records")
    comparison_year, previous_year = target_annual[:2]
    target_record = _annual_by_year(target)[comparison_year]
    if target_record.revenue is None:
        raise ValueError("target comparison-year revenue is missing")
    exclusions: list[PeerExclusion] = []
    ranking: list[tuple[Decimal, str, PeerCompany]] = []
    for candidate in candidates:
        reason = None
        if candidate.stock_code == target.stock_code:
            reason = "target company"
        elif not candidate.is_a_share:
            reason = "not an A-share company"
        elif candidate.csrc_industry != target.csrc_industry:
            reason = "different CSRC industry"
        elif candidate.is_st:
            reason = "ST or *ST company"
        elif candidate.is_financial:
            reason = "financial company"
        else:
            records = _annual_by_year(candidate)
            if comparison_year not in records or previous_year not in records:
                reason = "missing the two common complete annual periods"
            elif any(
                records[year].currency != "CNY" or records[year].unit != "CNY"
                for year in (comparison_year, previous_year)
            ):
                reason = "annual financial periods are not in CNY"
            elif records[comparison_year].revenue is None or records[previous_year].revenue is None:
                reason = "missing annual revenue"
            elif (
                records[comparison_year].gross_profit is None
                or records[comparison_year].parent_net_profit is None
            ):
                reason = "missing comparison-year profitability metrics"
            elif candidate.inventory_turnover is None:
                reason = "missing disclosed inventory turnover"
            elif candidate.inventory_turnover.period != f"FY{comparison_year}":
                reason = "inventory turnover period mismatch"
        if reason:
            exclusions.append(PeerExclusion(stock_code=candidate.stock_code, reason=reason))
            continue
        difference = abs(_annual_by_year(candidate)[comparison_year].revenue - target_record.revenue)
        ranking.append((difference, candidate.stock_code, candidate))
    ranking.sort(key=lambda item: (item[0], item[1]))
    selected = [item[2] for item in ranking[:3]]
    success = len(selected) == 3
    return PeerSelectionResult(
        success=success,
        comparison_year=comparison_year,
        previous_year=previous_year,
        selected=selected if success else [],
        ranking=[PeerRanking(stock_code=item[1], revenue_difference=item[0]) for item in ranking],
        exclusions=exclusions,
        failure_reason=None if success else "Fewer than three eligible same-industry A-share peers",
    )


def calculate_company_metrics(
    company: PeerCompany,
    *,
    comparison_year: int,
    previous_year: int,
) -> CompanyComparisonMetrics:
    records = _annual_by_year(company)
    current = records.get(comparison_year)
    previous = records.get(previous_year)
    if current is None or previous is None:
        raise ValueError(f"{company.stock_code}: missing common annual periods")
    if any(
        record.currency != "CNY" or record.unit != "CNY"
        for record in (current, previous)
    ):
        raise ValueError(f"{company.stock_code}: annual financial periods are not in CNY")
    missing = [
        name
        for name, value in (
            ("current revenue", current.revenue),
            ("previous revenue", previous.revenue),
            ("gross profit", current.gross_profit),
            ("attributable net profit", current.parent_net_profit),
        )
        if value is None
    ]
    if missing:
        raise ValueError(f"{company.stock_code}: missing " + ", ".join(missing))
    if current.revenue <= 0 or previous.revenue <= 0:
        raise ValueError(f"{company.stock_code}: annual revenue must be greater than zero")
    if company.inventory_turnover is None:
        raise ValueError(f"{company.stock_code}: missing disclosed inventory turnover")
    if company.inventory_turnover.period != f"FY{comparison_year}":
        raise ValueError(f"{company.stock_code}: inventory turnover period mismatch")
    quantize = lambda value: value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return CompanyComparisonMetrics(
        stock_code=company.stock_code,
        company_name=company.company_name,
        comparison_year=comparison_year,
        revenue_cny=current.revenue,
        gross_profit_cny=current.gross_profit,
        parent_net_profit_cny=current.parent_net_profit,
        revenue_growth=quantize((current.revenue - previous.revenue) / previous.revenue * 100),
        inventory_turnover=company.inventory_turnover.value,
        gross_margin=quantize(current.gross_profit / current.revenue * 100),
        net_margin=quantize(current.parent_net_profit / current.revenue * 100),
        revenue_source_url=current.source_url,
        previous_revenue_source_url=previous.source_url,
        inventory_turnover_source_url=company.inventory_turnover.source_url,
    )


def _table_rows(metrics: list[CompanyComparisonMetrics]) -> list[list[str]]:
    return [
        [
            item.company_name,
            f"CNY {item.revenue_cny / MILLION:,.2f} million",
            f"{item.inventory_turnover:.2f}",
            f"{item.gross_margin:.2f}%",
            f"{item.net_margin:.2f}%",
        ]
        for item in metrics
    ]


def _comparison_comment(metrics: list[CompanyComparisonMetrics]) -> str:
    target, *peers = metrics
    peer_growth = median(item.revenue_growth for item in peers)
    peer_gross = median(item.gross_margin for item in peers)
    peer_net = median(item.net_margin for item in peers)
    return (
        f"FY{target.comparison_year} comparison: the proposer recorded two-year revenue growth of "
        f"{target.revenue_growth:.2f}% versus a peer median of {peer_growth:.2f}%; gross margin was "
        f"{target.gross_margin:.2f}% versus {peer_gross:.2f}%; net margin was {target.net_margin:.2f}% "
        f"versus {peer_net:.2f}%."
    )


def collect_peer_analysis(
    *,
    target: PeerCompany,
    candidates: list[PeerCompany],
    captured_at: datetime,
) -> PeerAnalysisResult:
    selection = select_peers(target, candidates)
    if not selection.success:
        return PeerAnalysisResult(
            selection=selection,
            metrics=[],
            source_values=[],
            failures=[selection.failure_reason or "Peer selection failed"],
        )
    companies = [target, *selection.selected]
    try:
        metrics = [
            calculate_company_metrics(
                company,
                comparison_year=selection.comparison_year,
                previous_year=selection.previous_year,
            )
            for company in companies
        ]
    except ValueError as exc:
        return PeerAnalysisResult(
            selection=selection,
            metrics=[],
            source_values=[],
            failures=[str(exc)],
        )
    rows = _table_rows(metrics)
    item_evidence = [
        ItemEvidence(
            item_id=item.stock_code,
            source="derived",
            source_url=item.revenue_source_url,
            period=f"FY{item.comparison_year}",
            raw_value=item.model_dump(mode="json"),
        )
        for item in metrics
    ]
    formula_inputs = [
        derived
        for item in metrics
        for derived in (
            DerivedInput(field_id=f"{item.stock_code}.revenue", value=item.revenue_cny, period=f"FY{item.comparison_year}", source_url=item.revenue_source_url),
            DerivedInput(field_id=f"{item.stock_code}.previous_revenue", value=next(record.revenue for record in _annual_by_year(next(company for company in companies if company.stock_code == item.stock_code)).values() if record.fiscal_year == selection.previous_year), period=f"FY{selection.previous_year}", source_url=item.previous_revenue_source_url),
            DerivedInput(field_id=f"{item.stock_code}.gross_profit", value=item.gross_profit_cny, period=f"FY{item.comparison_year}", source_url=item.revenue_source_url),
            DerivedInput(field_id=f"{item.stock_code}.parent_net_profit", value=item.parent_net_profit_cny, period=f"FY{item.comparison_year}", source_url=item.revenue_source_url),
            DerivedInput(field_id=f"{item.stock_code}.inventory_turnover", value=item.inventory_turnover, period=f"FY{item.comparison_year}", source_url=item.inventory_turnover_source_url),
        )
    ]
    table_value = SourceValue(
        field_id="peer_comparison_table",
        value="\n".join(" | ".join(row) for row in rows),
        raw_value=[item.model_dump(mode="json") for item in metrics],
        source="derived",
        source_url=metrics[0].revenue_source_url,
        captured_at=captured_at,
        period=f"FY{selection.comparison_year}",
        metadata={"structured_value": rows},
        item_evidence=item_evidence,
        derived_evidence=DerivedEvidence(
            formula="revenue growth=(current-previous)/previous; gross margin=gross profit/revenue; net margin=attributable net profit/revenue",
            inputs=formula_inputs,
            result=rows,
        ),
    )
    comment = _comparison_comment(metrics)
    comment_value = SourceValue(
        field_id="peer_alignment_comment",
        value=comment,
        raw_value=[item.model_dump(mode="json") for item in metrics],
        source="derived",
        source_url=metrics[0].revenue_source_url,
        captured_at=captured_at,
        period=f"FY{selection.comparison_year}",
        item_evidence=item_evidence,
        derived_evidence=DerivedEvidence(
            formula="compare proposer metrics with median of the three selected peers",
            inputs=formula_inputs,
            result=comment,
        ),
    )
    return PeerAnalysisResult(
        selection=selection,
        metrics=metrics,
        source_values=[table_value, comment_value],
    )


def peer_analysis_node(state: WorkupAgentState) -> dict[str, Any]:
    part_results = dict(state.get("part_results", {}))
    part_04 = part_results.get("part_04")
    inputs = part_results.get("part_05_input")
    if not isinstance(part_04, dict) or not isinstance(inputs, dict):
        raise ValueError("part_results.part_04 and part_results.part_05_input are required")
    target_data = dict(inputs["target"])
    target_data["annual_records"] = part_04.get("annual_records", [])
    target = PeerCompany.model_validate(target_data)
    result = collect_peer_analysis(
        target=target,
        candidates=[PeerCompany.model_validate(item) for item in inputs.get("candidates", [])],
        captured_at=datetime.fromisoformat(state["created_at"]),
    )
    source_values = list(result.source_values)
    related = collect_related_party_transactions(
        [RelatedPartyTransaction.model_validate(item) for item in inputs.get("related_party_transactions", [])],
        captured_at=datetime.fromisoformat(state["created_at"]),
    )
    if related is not None:
        source_values.append(related)
    part_results["part_05"] = result.model_dump(mode="json")
    return {
        "part_results": part_results,
        "source_values": list(state.get("source_values", []))
        + [item.model_dump(mode="json") for item in source_values],
        "node_errors": list(state.get("node_errors", []))
        + [{"node": "part_05", "message": message} for message in result.failures],
    }
