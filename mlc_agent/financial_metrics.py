from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from mlc_agent.operating_performance import FinancialPeriodRecord
from mlc_agent.schemas import (
    DerivedEvidence,
    DerivedInput,
    FailedField,
    SourceValue,
    WorkupAgentState,
)


MILLION = Decimal("1000000")
TWO_PLACES = Decimal("0.01")


class MetricInput(BaseModel):
    field_id: str = Field(min_length=1)
    value: Decimal
    period: str = Field(min_length=1)
    currency: Literal["CNY"] = "CNY"
    unit: Literal["CNY"] = "CNY"
    source_url: str


class AnnualLiquidityInput(BaseModel):
    financial_period: FinancialPeriodRecord
    current_assets: MetricInput | None = None
    current_liabilities: MetricInput | None = None
    inventory: MetricInput | None = None
    capex_cash_paid: MetricInput | None = None
    operating_profit: MetricInput | None = None
    interest_expense: MetricInput | None = None
    total_liabilities: MetricInput | None = None
    total_assets: MetricInput | None = None
    operating_cash_flow: MetricInput | None = None

    @model_validator(mode="after")
    def require_annual_period(self) -> "AnnualLiquidityInput":
        if self.financial_period.period_type != "annual":
            raise ValueError("liquidity metrics require complete annual periods")
        if self.financial_period.currency != "CNY" or self.financial_period.unit != "CNY":
            raise ValueError("financial period currency and unit must both be CNY")
        expected_period = f"FY{self.financial_period.fiscal_year}"
        for name in (
            "current_assets",
            "current_liabilities",
            "inventory",
            "capex_cash_paid",
            "operating_profit",
            "interest_expense",
            "total_liabilities",
            "total_assets",
            "operating_cash_flow",
        ):
            metric = getattr(self, name)
            if metric is not None and metric.period != expected_period:
                raise ValueError(
                    f"{name} period {metric.period} does not match {expected_period}"
                )
        return self


class LatestBalanceSheetInput(BaseModel):
    period: str = Field(min_length=1)
    monetary_funds: MetricInput
    short_term_borrowings: MetricInput

    @model_validator(mode="after")
    def validate_metric_periods(self) -> "LatestBalanceSheetInput":
        for name in ("monetary_funds", "short_term_borrowings"):
            metric = getattr(self, name)
            if metric.period != self.period:
                raise ValueError(
                    f"{name} period {metric.period} does not match {self.period}"
                )
        if (
            self.monetary_funds.currency != self.short_term_borrowings.currency
            or self.monetary_funds.unit != self.short_term_borrowings.unit
        ):
            raise ValueError("latest balance-sheet metrics must use the same currency and unit")
        return self


class MetricFailure(BaseModel):
    field_id: str
    period: str
    reason: str


class PeriodMetrics(BaseModel):
    fiscal_year: int
    current_ratio: Decimal | None = None
    quick_ratio: Decimal | None = None
    capex_cny_million: Decimal | None = None
    capex_to_revenue: Decimal | None = None
    interest_coverage: Decimal | None = None
    debt_to_asset: Decimal | None = None
    ocf_positive: bool | None = None
    cash_flow_gt_noi: bool | None = None


class FinancialMetricsResult(BaseModel):
    source_values: list[SourceValue]
    periods: list[PeriodMetrics]
    short_term_debt_pressure: bool
    sustained_positive_ocf: bool | None
    failures: list[MetricFailure]


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _period(record: FinancialPeriodRecord) -> str:
    return f"FY{record.fiscal_year}"


def _input(metric: MetricInput) -> DerivedInput:
    return DerivedInput(
        field_id=metric.field_id,
        value=metric.value,
        period=metric.period,
        source_url=metric.source_url,
    )


def calculate_ratio(
    *,
    numerator: Decimal,
    denominator: Decimal,
    denominator_name: str,
) -> Decimal:
    if denominator <= 0:
        raise ValueError(f"{denominator_name} must be greater than zero")
    return _round(numerator / denominator)


def normalize_capex(value: Decimal) -> Decimal:
    return abs(value)


def calculate_current_ratio(current_assets: Decimal, current_liabilities: Decimal) -> Decimal:
    if current_assets < 0:
        raise ValueError("current assets must not be negative")
    return calculate_ratio(
        numerator=current_assets,
        denominator=current_liabilities,
        denominator_name="current liabilities",
    )


def calculate_quick_ratio(
    current_assets: Decimal,
    inventory: Decimal,
    current_liabilities: Decimal,
) -> Decimal:
    if current_assets < 0 or inventory < 0:
        raise ValueError("current assets and inventory must not be negative")
    return calculate_ratio(
        numerator=current_assets - inventory,
        denominator=current_liabilities,
        denominator_name="current liabilities",
    )


def calculate_capex_to_revenue(capex: Decimal, revenue: Decimal) -> Decimal:
    return calculate_ratio(
        numerator=normalize_capex(capex),
        denominator=revenue,
        denominator_name="revenue",
    )


def calculate_interest_coverage(operating_profit: Decimal, interest_expense: Decimal) -> Decimal:
    normalized_interest = abs(interest_expense)
    return calculate_ratio(
        numerator=operating_profit + normalized_interest,
        denominator=normalized_interest,
        denominator_name="interest expense",
    )


def calculate_debt_to_asset(total_liabilities: Decimal, total_assets: Decimal) -> Decimal:
    if total_liabilities < 0:
        raise ValueError("total liabilities must not be negative")
    return calculate_ratio(
        numerator=total_liabilities,
        denominator=total_assets,
        denominator_name="total assets",
    )


def _derived_value(
    *,
    field_id: str,
    text: str,
    raw_value: Any,
    formula: str,
    inputs: list[DerivedInput],
    result: Any,
    captured_at: datetime,
    period: str,
) -> SourceValue:
    return SourceValue(
        field_id=field_id,
        value=text,
        raw_value=raw_value,
        source="derived",
        source_url=inputs[0].source_url,
        captured_at=captured_at,
        period=period,
        derived_evidence=DerivedEvidence(formula=formula, inputs=inputs, result=result),
    )


def _missing(record: AnnualLiquidityInput, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if getattr(record, name) is None]


def calculate_period_metrics(
    record: AnnualLiquidityInput,
    *,
    suffix: str,
    captured_at: datetime,
) -> tuple[PeriodMetrics, list[SourceValue], list[MetricFailure]]:
    period = _period(record.financial_period)
    values: list[SourceValue] = []
    failures: list[MetricFailure] = []
    output = PeriodMetrics(fiscal_year=record.financial_period.fiscal_year)

    def fail(field_id: str, reason: str) -> None:
        failures.append(MetricFailure(field_id=field_id, period=period, reason=reason))

    field_id = f"current_ratio_{suffix}"
    missing = _missing(record, ("current_assets", "current_liabilities"))
    if missing:
        fail(field_id, "Missing inputs: " + ", ".join(missing))
    else:
        try:
            result = calculate_current_ratio(record.current_assets.value, record.current_liabilities.value)
            output.current_ratio = result
            inputs = [_input(record.current_assets), _input(record.current_liabilities)]
            values.append(_derived_value(field_id=field_id, text=f"{result:.2f}", raw_value=result, formula="current_assets / current_liabilities", inputs=inputs, result=result, captured_at=captured_at, period=period))
        except ValueError as exc:
            fail(field_id, str(exc))

    field_id = f"quick_ratio_{suffix}"
    missing = _missing(record, ("current_assets", "inventory", "current_liabilities"))
    if missing:
        fail(field_id, "Missing inputs: " + ", ".join(missing))
    else:
        try:
            result = calculate_quick_ratio(record.current_assets.value, record.inventory.value, record.current_liabilities.value)
            output.quick_ratio = result
            inputs = [_input(record.current_assets), _input(record.inventory), _input(record.current_liabilities)]
            values.append(_derived_value(field_id=field_id, text=f"{result:.2f}", raw_value=result, formula="(current_assets - inventory) / current_liabilities", inputs=inputs, result=result, captured_at=captured_at, period=period))
        except ValueError as exc:
            fail(field_id, str(exc))

    field_id = f"capex_{suffix}"
    if record.capex_cash_paid is None:
        fail(field_id, "Missing inputs: capex_cash_paid")
    else:
        capex = normalize_capex(record.capex_cash_paid.value)
        output.capex_cny_million = _round(capex / MILLION)
        inputs = [_input(record.capex_cash_paid)]
        values.append(_derived_value(field_id=field_id, text=f"CNY {output.capex_cny_million:,.2f} million", raw_value=capex, formula="abs(cash_paid_for_fixed_intangible_and_other_long_term_assets) / 1000000", inputs=inputs, result=output.capex_cny_million, captured_at=captured_at, period=period))

    field_id = f"capex_to_revenue_{suffix}"
    if record.capex_cash_paid is None or record.financial_period.revenue is None:
        missing_names = ["capex_cash_paid" if record.capex_cash_paid is None else None, "revenue" if record.financial_period.revenue is None else None]
        fail(field_id, "Missing inputs: " + ", ".join(item for item in missing_names if item))
    else:
        try:
            result = calculate_capex_to_revenue(record.capex_cash_paid.value, record.financial_period.revenue)
            output.capex_to_revenue = result
            revenue_input = DerivedInput(field_id="revenue", value=record.financial_period.revenue, period=period, source_url=record.financial_period.source_url)
            inputs = [_input(record.capex_cash_paid), revenue_input]
            values.append(_derived_value(field_id=field_id, text=f"{result:.2f}", raw_value=result, formula="abs(capex_cash_paid) / revenue", inputs=inputs, result=result, captured_at=captured_at, period=period))
        except ValueError as exc:
            fail(field_id, str(exc))

    field_id = f"interest_coverage_{suffix}"
    missing = _missing(record, ("operating_profit", "interest_expense"))
    if missing:
        fail(field_id, "Missing inputs: " + ", ".join(missing))
    else:
        try:
            interest = abs(record.interest_expense.value)
            result = calculate_interest_coverage(record.operating_profit.value, record.interest_expense.value)
            output.interest_coverage = result
            inputs = [_input(record.operating_profit), _input(record.interest_expense)]
            values.append(_derived_value(field_id=field_id, text=f"{result:.2f}", raw_value=result, formula="(operating_profit + abs(interest_expense)) / abs(interest_expense)", inputs=inputs, result=result, captured_at=captured_at, period=period))
        except ValueError as exc:
            fail(field_id, str(exc))

    field_id = f"debt_to_asset_{suffix}"
    missing = _missing(record, ("total_liabilities", "total_assets"))
    if missing:
        fail(field_id, "Missing inputs: " + ", ".join(missing))
    else:
        try:
            result = calculate_debt_to_asset(record.total_liabilities.value, record.total_assets.value)
            output.debt_to_asset = result
            inputs = [_input(record.total_liabilities), _input(record.total_assets)]
            values.append(_derived_value(field_id=field_id, text=f"{result:.2f}", raw_value=result, formula="total_liabilities / total_assets", inputs=inputs, result=result, captured_at=captured_at, period=period))
        except ValueError as exc:
            fail(field_id, str(exc))

    field_id = f"ocf_positive_{suffix}"
    if record.operating_cash_flow is None:
        fail(field_id, "Missing inputs: operating_cash_flow")
    else:
        result = record.operating_cash_flow.value > 0
        output.ocf_positive = result
        inputs = [_input(record.operating_cash_flow)]
        values.append(_derived_value(field_id=field_id, text="Yes" if result else "No", raw_value=result, formula="operating_cash_flow > 0", inputs=inputs, result=result, captured_at=captured_at, period=period))

    field_id = f"cash_flow_gt_noi_{suffix}"
    if record.operating_cash_flow is None or record.financial_period.parent_net_profit is None:
        missing_names = ["operating_cash_flow" if record.operating_cash_flow is None else None, "parent_net_profit" if record.financial_period.parent_net_profit is None else None]
        fail(field_id, "Missing inputs: " + ", ".join(item for item in missing_names if item))
    else:
        result = record.operating_cash_flow.value > record.financial_period.parent_net_profit
        output.cash_flow_gt_noi = result
        profit_input = DerivedInput(field_id="parent_net_profit", value=record.financial_period.parent_net_profit, period=period, source_url=record.financial_period.source_url)
        inputs = [_input(record.operating_cash_flow), profit_input]
        values.append(_derived_value(field_id=field_id, text="Yes" if result else "No", raw_value=result, formula="operating_cash_flow > parent_net_profit", inputs=inputs, result=result, captured_at=captured_at, period=period))
    return output, values, failures


def _trend(name: str, previous: Decimal | None, current: Decimal | None) -> str | None:
    if previous is None or current is None:
        return None
    direction = "increased" if current > previous else "decreased" if current < previous else "was unchanged"
    return f"{name} {direction} from {previous:.2f} to {current:.2f}."


def collect_financial_metrics(
    *,
    annual_records: list[AnnualLiquidityInput],
    latest_balance_sheet: LatestBalanceSheetInput,
    captured_at: datetime,
) -> FinancialMetricsResult:
    ordered = sorted(annual_records, key=lambda item: item.financial_period.fiscal_year, reverse=True)
    if len(ordered) != 2:
        raise ValueError("exactly the latest two complete annual records are required")
    if ordered[0].financial_period.fiscal_year - ordered[1].financial_period.fiscal_year != 1:
        raise ValueError("annual records must be consecutive fiscal years")
    source_values: list[SourceValue] = []
    failures: list[MetricFailure] = []
    periods: list[PeriodMetrics] = []
    for suffix, record in (("current", ordered[0]), ("previous", ordered[1])):
        period_result, values, period_failures = calculate_period_metrics(
            record, suffix=suffix, captured_at=captured_at
        )
        periods.append(period_result)
        source_values.extend(values)
        failures.extend(period_failures)

    cash = latest_balance_sheet.monetary_funds
    debt = latest_balance_sheet.short_term_borrowings
    if cash.value < 0 or debt.value < 0:
        raise ValueError("monetary funds and short-term borrowings must not be negative")
    pressure = cash.value < debt.value
    pressure_text = "Yes" if pressure else "No"
    if pressure:
        pressure_text += (
            f" - monetary funds CNY {_round(cash.value / MILLION):,.2f} million; "
            f"short-term borrowings CNY {_round(debt.value / MILLION):,.2f} million."
        )
    pressure_inputs = [_input(cash), _input(debt)]
    source_values.append(_derived_value(field_id="short_term_debt_pressure", text=pressure_text, raw_value=pressure, formula="monetary_funds < short_term_borrowings", inputs=pressure_inputs, result=pressure, captured_at=captured_at, period=latest_balance_sheet.period))

    ocf_values = [item.operating_cash_flow for item in ordered]
    sustained: bool | None = None
    if any(item is None for item in ocf_values):
        failures.append(MetricFailure(field_id="sustained_positive_ocf", period="latest two full years", reason="Missing operating_cash_flow for one or both annual periods"))
    else:
        sustained = all(item.value > 0 for item in ocf_values)
        inputs = [_input(item) for item in ocf_values]
        source_values.append(_derived_value(field_id="sustained_positive_ocf", text="Yes" if sustained else "No", raw_value=sustained, formula="current_year_ocf > 0 and previous_year_ocf > 0", inputs=inputs, result=sustained, captured_at=captured_at, period="latest two full years"))

    current_record = ordered[0]
    if (
        current_record.operating_cash_flow is None
        or current_record.financial_period.parent_net_profit is None
    ):
        failures.append(
            MetricFailure(
                field_id="net_income_exceeds_ocf",
                period=_period(current_record.financial_period),
                reason="Missing operating_cash_flow or parent_net_profit",
            )
        )
    else:
        net_income_exceeds = (
            current_record.financial_period.parent_net_profit
            > current_record.operating_cash_flow.value
        )
        inputs = [
            DerivedInput(
                field_id="parent_net_profit",
                value=current_record.financial_period.parent_net_profit,
                period=_period(current_record.financial_period),
                source_url=current_record.financial_period.source_url,
            ),
            _input(current_record.operating_cash_flow),
        ]
        source_values.append(
            _derived_value(
                field_id="net_income_exceeds_ocf",
                text="Yes" if net_income_exceeds else "No",
                raw_value=net_income_exceeds,
                formula="parent_net_profit > operating_cash_flow",
                inputs=inputs,
                result=net_income_exceeds,
                captured_at=captured_at,
                period=_period(current_record.financial_period),
            )
        )

    current = periods[0]
    previous = periods[1]
    comments = [
        item
        for item in (
            _trend("Current ratio", previous.current_ratio, current.current_ratio),
            _trend("Quick ratio", previous.quick_ratio, current.quick_ratio),
            _trend("Debt-to-asset ratio", previous.debt_to_asset, current.debt_to_asset),
        )
        if item
    ]
    comments.append(f"Short-term debt pressure: {'Yes' if pressure else 'No'}.")
    if sustained is not None:
        comments.append(f"Operating cash flow was positive in both full years: {'Yes' if sustained else 'No'}.")
    comment_dependencies = {
        "current_ratio_previous",
        "current_ratio_current",
        "quick_ratio_previous",
        "quick_ratio_current",
        "debt_to_asset_previous",
        "debt_to_asset_current",
        "short_term_debt_pressure",
        "sustained_positive_ocf",
    }
    comment_inputs = [
        DerivedInput(
            field_id=item.field_id,
            value=item.value,
            period=item.period,
            source_url=item.source_url,
        )
        for item in source_values
        if item.field_id in comment_dependencies
    ]
    source_values.append(_derived_value(field_id="liquidity_comment", text=" ".join(comments), raw_value={"current": current, "previous": previous, "pressure": pressure, "sustained_positive_ocf": sustained}, formula="summary of calculated period trends and boolean tests", inputs=comment_inputs, result=" ".join(comments), captured_at=captured_at, period="latest two full years and latest balance sheet"))
    return FinancialMetricsResult(source_values=source_values, periods=periods, short_term_debt_pressure=pressure, sustained_positive_ocf=sustained, failures=failures)


def financial_metrics_node(state: WorkupAgentState) -> dict[str, Any]:
    part_results = dict(state.get("part_results", {}))
    operating = part_results.get("part_04")
    inputs = part_results.get("part_06_input")
    if not isinstance(operating, dict) or not isinstance(inputs, dict):
        raise ValueError("part_results.part_04 and part_results.part_06_input are required")
    annual_by_year = {
        record.fiscal_year: record
        for record in (
            FinancialPeriodRecord.model_validate(item)
            for item in operating.get("annual_records", [])
        )
    }
    annual_inputs: list[AnnualLiquidityInput] = []
    for item in inputs.get("annual_metrics", []):
        fiscal_year = int(item["fiscal_year"])
        if fiscal_year not in annual_by_year:
            raise ValueError(f"part_04 annual record is missing for FY{fiscal_year}")
        annual_inputs.append(
            AnnualLiquidityInput.model_validate(
                {**item, "financial_period": annual_by_year[fiscal_year]}
            )
        )
    result = collect_financial_metrics(
        annual_records=annual_inputs,
        latest_balance_sheet=LatestBalanceSheetInput.model_validate(inputs["latest_balance_sheet"]),
        captured_at=datetime.fromisoformat(state["created_at"]),
    )
    part_results["part_06"] = result.model_dump(mode="json")
    return {
        "part_results": part_results,
        "source_values": list(state.get("source_values", []))
        + [item.model_dump(mode="json") for item in result.source_values],
        "failed_fields": list(state.get("failed_fields", []))
        + [
            FailedField(
                field_id=item.field_id,
                label=item.field_id.replace("_", " ").title(),
                reason=f"{item.period}: {item.reason}",
                source_attempted=["derived"],
            ).model_dump(mode="json")
            for item in result.failures
        ],
    }
