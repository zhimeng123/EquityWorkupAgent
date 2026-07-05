from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from mlc_agent.operating_performance import FinancialPeriodRecord
from mlc_agent.schemas import (
    DerivedEvidence,
    DerivedInput,
    ItemEvidence,
    SourceValue,
    WorkupAgentState,
)


class AnnualBalanceSheetDetails(BaseModel):
    fiscal_year: int
    report_date: date
    accounts_receivable: Decimal = Field(ge=0)
    accounts_receivable_gross: Decimal = Field(ge=0)
    goodwill: Decimal = Field(ge=0)
    intangible_assets: Decimal = Field(ge=0)
    total_assets: Decimal = Field(gt=0)
    currency: str = "CNY"
    unit: str = "CNY"
    consolidation_scope_id: str = Field(min_length=1)
    source_url: str
    page_number: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_annual_period(self) -> "AnnualBalanceSheetDetails":
        if self.report_date != date(self.fiscal_year, 12, 31):
            raise ValueError("annual balance sheet must end on fiscal year 31 December")
        if self.currency != "CNY" or self.unit != "CNY":
            raise ValueError("annual balance sheet values must be normalized to CNY")
        if self.accounts_receivable_gross < self.accounts_receivable:
            raise ValueError("gross accounts receivable cannot be below carrying amount")
        return self


class AnnualReceivableDetails(BaseModel):
    """Minimal, comparable balance-sheet facts needed for the growth field."""

    fiscal_year: int
    report_date: date
    accounts_receivable: Decimal = Field(ge=0)
    currency: str = "CNY"
    unit: str = "CNY"
    consolidation_scope_id: str = Field(min_length=1)
    source_url: str
    page_number: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_annual_period(self) -> "AnnualReceivableDetails":
        if self.report_date != date(self.fiscal_year, 12, 31):
            raise ValueError("annual receivable balance must end on fiscal year 31 December")
        if self.currency != "CNY" or self.unit != "CNY":
            raise ValueError("annual receivable values must be normalized to CNY")
        return self


ImpairmentCategory = Literal[
    "accounts_receivable_bad_debt",
    "receivable_credit_impairment",
    "contract_asset_impairment",
    "other_asset_impairment",
]


class ImpairmentComponent(BaseModel):
    component_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: ImpairmentCategory
    amount: Decimal = Field(ge=0)
    fiscal_year: int
    currency: str = "CNY"
    unit: str = "CNY"
    officially_material: bool = False
    officially_one_time: bool = False
    source: Literal["annual_report", "cninfo", "exchange"]
    source_url: str
    evidence_date: date
    page_number: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_unit(self) -> "ImpairmentComponent":
        if self.currency != "CNY" or self.unit != "CNY":
            raise ValueError("impairment values must be normalized to CNY")
        return self


class ReceivablesGrowthResult(BaseModel):
    receivables_growth: Decimal
    revenue_growth: Decimal
    receivables_growth_higher: bool


class ImpairmentAnalysisResult(BaseModel):
    included_components: list[ImpairmentComponent]
    total_impairment: Decimal
    impairment_to_revenue: Decimal
    impairment_to_gross_receivables: Decimal
    material_one_time_impairment: bool
    material_basis: str


class IntangiblesAnalysisResult(BaseModel):
    combined_intangibles: Decimal
    ratio: Decimal
    exceeds_25_percent: bool


class DeepFinancialAnalysisResult(BaseModel):
    source_values: list[SourceValue]
    receivables_growth: ReceivablesGrowthResult | None = None
    impairment_analysis: ImpairmentAnalysisResult | None = None
    intangibles_analysis: IntangiblesAnalysisResult | None = None
    errors: list[str] = Field(default_factory=list)


def calculate_growth(current: Decimal, previous: Decimal, *, metric: str) -> Decimal:
    if previous == 0:
        raise ValueError(f"{metric} previous-year value is zero")
    return (current - previous) / previous


def compare_receivables_and_revenue_growth(
    current_financial: FinancialPeriodRecord,
    previous_financial: FinancialPeriodRecord,
    current_balance: AnnualBalanceSheetDetails | AnnualReceivableDetails,
    previous_balance: AnnualBalanceSheetDetails | AnnualReceivableDetails,
) -> ReceivablesGrowthResult:
    if current_financial.period_type != "annual" or previous_financial.period_type != "annual":
        raise ValueError("receivables growth requires two annual financial records")
    if current_financial.fiscal_year != current_balance.fiscal_year or previous_financial.fiscal_year != previous_balance.fiscal_year:
        raise ValueError("financial and balance-sheet periods do not match")
    if current_financial.fiscal_year != previous_financial.fiscal_year + 1:
        raise ValueError("annual periods must be consecutive")
    if current_balance.consolidation_scope_id != previous_balance.consolidation_scope_id:
        raise ValueError("annual consolidation scopes do not match")
    if current_financial.revenue is None or previous_financial.revenue is None:
        raise ValueError("annual revenue is missing")
    receivables_growth = calculate_growth(
        current_balance.accounts_receivable,
        previous_balance.accounts_receivable,
        metric="accounts receivable",
    )
    revenue_growth = calculate_growth(
        current_financial.revenue,
        previous_financial.revenue,
        metric="revenue",
    )
    return ReceivablesGrowthResult(
        receivables_growth=receivables_growth,
        revenue_growth=revenue_growth,
        receivables_growth_higher=receivables_growth > revenue_growth,
    )


_INCLUDED_IMPAIRMENT_CATEGORIES = {
    "accounts_receivable_bad_debt",
    "receivable_credit_impairment",
    "contract_asset_impairment",
}


def analyze_receivable_impairment(
    components: list[ImpairmentComponent],
    latest_financial: FinancialPeriodRecord,
    latest_balance: AnnualBalanceSheetDetails,
) -> ImpairmentAnalysisResult:
    if latest_financial.period_type != "annual":
        raise ValueError("impairment analysis requires an annual financial record")
    if latest_financial.fiscal_year != latest_balance.fiscal_year:
        raise ValueError("impairment and balance-sheet periods do not match")
    included = [
        item
        for item in components
        if item.fiscal_year == latest_financial.fiscal_year
        and item.category in _INCLUDED_IMPAIRMENT_CATEGORIES
    ]
    if not included:
        raise ValueError("no eligible receivable-related impairment components")
    if latest_balance.accounts_receivable_gross == 0:
        raise ValueError("period-end gross accounts receivable is zero")
    if latest_financial.revenue in (None, Decimal("0")):
        raise ValueError("annual revenue is missing or zero")
    total = sum((item.amount for item in included), Decimal("0"))
    ratio = total / latest_balance.accounts_receivable_gross
    revenue_ratio = total / latest_financial.revenue
    qualitative = any(item.officially_material or item.officially_one_time for item in included)
    profit = latest_financial.parent_net_profit
    if profit is None:
        raise ValueError("annual attributable net profit is missing")
    if profit > 0 and total >= abs(profit) * Decimal("0.10"):
        material = True
        basis = "quantitative: total impairment >= 10% of absolute attributable net profit"
    elif qualitative:
        material = True
        basis = "qualitative: formally disclosed as material or one-time"
    elif profit <= 0:
        material = False
        basis = "not material: non-positive attributable net profit permits formal qualitative basis only"
    else:
        material = False
        basis = "not material: below quantitative threshold and no formal qualitative designation"
    return ImpairmentAnalysisResult(
        included_components=included,
        total_impairment=total,
        impairment_to_revenue=revenue_ratio,
        impairment_to_gross_receivables=ratio,
        material_one_time_impairment=material,
        material_basis=basis,
    )


def analyze_intangibles(balance: AnnualBalanceSheetDetails) -> IntangiblesAnalysisResult:
    combined = balance.goodwill + balance.intangible_assets
    ratio = combined / balance.total_assets
    return IntangiblesAnalysisResult(
        combined_intangibles=combined,
        ratio=ratio,
        exceeds_25_percent=ratio > Decimal("0.25"),
    )


def _input(field_id: str, value: Any, period: str, url: str) -> DerivedInput:
    return DerivedInput(field_id=field_id, value=value, period=period, source_url=url)


def _component_evidence_period(item: ImpairmentComponent) -> str:
    return (
        f"FY{item.fiscal_year}; evidence date {item.evidence_date.isoformat()}, "
        f"page {item.page_number}"
    )


def _select_two_annual_records(
    annual_records: list[FinancialPeriodRecord],
) -> tuple[FinancialPeriodRecord, FinancialPeriodRecord]:
    annual = sorted(
        (item for item in annual_records if item.period_type == "annual"),
        key=lambda item: item.fiscal_year,
        reverse=True,
    )
    if len(annual) < 2:
        raise ValueError("two complete annual financial records are required")
    if annual[0].fiscal_year != annual[1].fiscal_year + 1:
        raise ValueError("two latest complete annual periods must be consecutive")
    return annual[0], annual[1]


def collect_deep_financial_analysis(
    *,
    annual_records: list[FinancialPeriodRecord],
    balance_sheet_details: list[AnnualBalanceSheetDetails],
    receivable_details: list[AnnualReceivableDetails] | None = None,
    impairment_components: list[ImpairmentComponent],
    captured_at: datetime,
) -> DeepFinancialAnalysisResult:
    values: list[SourceValue] = []
    errors: list[str] = []
    growth_result = None
    impairment_result = None
    intangibles_result = None
    try:
        current_financial, previous_financial = _select_two_annual_records(annual_records)
        balances = {
            item.fiscal_year: item
            for item in (receivable_details if receivable_details is not None else balance_sheet_details)
        }
        current_balance = balances[current_financial.fiscal_year]
        previous_balance = balances[previous_financial.fiscal_year]
        growth_result = compare_receivables_and_revenue_growth(
            current_financial,
            previous_financial,
            current_balance,
            previous_balance,
        )
        growth_inputs = [
            _input("accounts_receivable_current", current_balance.accounts_receivable, f"FY{current_balance.fiscal_year}, page {current_balance.page_number}", current_balance.source_url),
            _input("accounts_receivable_previous", previous_balance.accounts_receivable, f"FY{previous_balance.fiscal_year}, page {previous_balance.page_number}", previous_balance.source_url),
            _input("revenue_current", current_financial.revenue, f"FY{current_financial.fiscal_year}", current_financial.source_url),
            _input("revenue_previous", previous_financial.revenue, f"FY{previous_financial.fiscal_year}", previous_financial.source_url),
        ]
        values.append(
            SourceValue(
                field_id="receivables_vs_revenue_growth",
                value=(
                    f"{'Yes' if growth_result.receivables_growth_higher else 'No'}. "
                    f"Accounts receivable growth was {growth_result.receivables_growth:.2%}; "
                    f"revenue growth was {growth_result.revenue_growth:.2%}."
                ),
                raw_value=growth_result.model_dump(mode="json"),
                source="derived",
                source_url=current_balance.source_url,
                captured_at=captured_at,
                period=f"FY{previous_financial.fiscal_year}-FY{current_financial.fiscal_year}",
                derived_evidence=DerivedEvidence(
                    formula="AR growth = (AR current - AR previous) / AR previous; revenue growth = (revenue current - revenue previous) / revenue previous; result = AR growth > revenue growth",
                    inputs=growth_inputs,
                    result=growth_result.model_dump(mode="json"),
                ),
            )
        )
    except KeyError as exc:
        errors.append(
            "receivables_vs_revenue_growth: missing annual accounts receivable for "
            f"FY{exc.args[0]}"
        )
    except ValueError as exc:
        errors.append(f"receivables_vs_revenue_growth: {exc}")

    try:
        current_financial, _ = _select_two_annual_records(annual_records)
        current_balance = next(
            item for item in balance_sheet_details if item.fiscal_year == current_financial.fiscal_year
        )
        impairment_result = analyze_receivable_impairment(
            impairment_components,
            current_financial,
            current_balance,
        )
        impairment_inputs = [
            *[
                _input(f"impairment_component:{item.component_id}", item.amount, _component_evidence_period(item), item.source_url)
                for item in impairment_result.included_components
            ],
            _input("accounts_receivable_gross", current_balance.accounts_receivable_gross, f"FY{current_balance.fiscal_year}, page {current_balance.page_number}", current_balance.source_url),
            _input("annual_revenue", current_financial.revenue, f"FY{current_financial.fiscal_year}", current_financial.source_url),
            _input("attributable_net_profit", current_financial.parent_net_profit, f"FY{current_financial.fiscal_year}", current_financial.source_url),
        ]
        values.append(
            SourceValue(
                field_id="receivable_impairment_analysis",
                value=(
                    f"{'Yes' if impairment_result.material_one_time_impairment else 'No'}. "
                    f"Eligible receivable-related impairment totalled CNY {impairment_result.total_impairment / Decimal('1000000'):,.2f} million, "
                    f"equal to {impairment_result.impairment_to_revenue:.2%} of annual revenue and "
                    f"{impairment_result.impairment_to_gross_receivables:.2%} of period-end gross accounts receivable. "
                    f"Basis: {impairment_result.material_basis}."
                ),
                raw_value=impairment_result.model_dump(mode="json"),
                source="derived",
                source_url=impairment_result.included_components[0].source_url,
                captured_at=captured_at,
                period=f"FY{current_financial.fiscal_year}",
                derived_evidence=DerivedEvidence(
                    formula="eligible impairment total = sum(receivable bad debt + receivable credit impairment + contract asset impairment); revenue ratio = eligible total / annual revenue; receivables ratio = eligible total / period-end gross accounts receivable; material = formal material/one-time designation OR (attributable net profit > 0 AND eligible total >= abs(attributable net profit) * 10%)",
                    inputs=impairment_inputs,
                    result=impairment_result.model_dump(mode="json"),
                ),
                item_evidence=[
                    ItemEvidence(
                        item_id=item.component_id,
                        source=item.source,
                        source_url=item.source_url,
                        period=_component_evidence_period(item),
                        raw_value=item.model_dump(mode="json"),
                    )
                    for item in impairment_result.included_components
                ],
            )
        )
    except (StopIteration, ValueError) as exc:
        errors.append(f"receivable_impairment_analysis: {exc}")

    try:
        current_financial, _ = _select_two_annual_records(annual_records)
        current_balance = next(
            item for item in balance_sheet_details if item.fiscal_year == current_financial.fiscal_year
        )
        intangibles_result = analyze_intangibles(current_balance)
        values.append(
            SourceValue(
                field_id="intangibles_asset_analysis",
                value=(
                    f"{'Yes' if intangibles_result.exceeds_25_percent else 'No'}. "
                    f"Goodwill plus intangible assets represented {intangibles_result.ratio:.2%} of total assets."
                ),
                raw_value=intangibles_result.model_dump(mode="json"),
                source="derived",
                source_url=current_balance.source_url,
                captured_at=captured_at,
                period=f"FY{current_balance.fiscal_year}",
                derived_evidence=DerivedEvidence(
                    formula="intangibles ratio = (goodwill + intangible assets) / total assets; result = ratio > 25%",
                    inputs=[
                        _input("goodwill", current_balance.goodwill, f"FY{current_balance.fiscal_year}, page {current_balance.page_number}", current_balance.source_url),
                        _input("intangible_assets", current_balance.intangible_assets, f"FY{current_balance.fiscal_year}, page {current_balance.page_number}", current_balance.source_url),
                        _input("total_assets", current_balance.total_assets, f"FY{current_balance.fiscal_year}, page {current_balance.page_number}", current_balance.source_url),
                    ],
                    result=intangibles_result.model_dump(mode="json"),
                ),
            )
        )
    except (StopIteration, ValueError) as exc:
        errors.append(f"intangibles_asset_analysis: {exc}")
    return DeepFinancialAnalysisResult(
        source_values=values,
        receivables_growth=growth_result,
        impairment_analysis=impairment_result,
        intangibles_analysis=intangibles_result,
        errors=errors,
    )


def deep_financial_analysis_node(state: WorkupAgentState) -> dict[str, Any]:
    inputs = state.get("part_results", {}).get("part_07_input")
    if not isinstance(inputs, dict):
        raise ValueError("part_results.part_07_input is required")
    part_04 = state.get("part_results", {}).get("part_04")
    if not isinstance(part_04, dict):
        raise ValueError("part_results.part_04 annual_records are required")
    result = collect_deep_financial_analysis(
        annual_records=[FinancialPeriodRecord.model_validate(item) for item in part_04.get("annual_records", [])],
        balance_sheet_details=[AnnualBalanceSheetDetails.model_validate(item) for item in inputs.get("balance_sheet_details", [])],
        impairment_components=[ImpairmentComponent.model_validate(item) for item in inputs.get("impairment_components", [])],
        captured_at=datetime.fromisoformat(state["created_at"]),
    )
    part_results = dict(state.get("part_results", {}))
    part_results["part_07"] = result.model_dump(mode="json")
    return {
        "part_results": part_results,
        "source_values": list(state.get("source_values", [])) + [item.model_dump(mode="json") for item in result.source_values],
        "node_errors": list(state.get("node_errors", [])) + [{"node": "part_07", "message": item} for item in result.errors],
    }
