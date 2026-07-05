from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from mlc_agent.config import load_yaml
from mlc_agent.docx_writer import validate_template_mapping
from mlc_agent.financial_metrics import (
    AnnualLiquidityInput,
    LatestBalanceSheetInput,
    MetricInput,
    calculate_current_ratio,
    calculate_debt_to_asset,
    calculate_interest_coverage,
    calculate_period_metrics,
    calculate_quick_ratio,
    collect_financial_metrics,
    financial_metrics_node,
    normalize_capex,
)
from mlc_agent.operating_performance import FinancialPeriodRecord
from mlc_agent.schemas import FieldMapping


ROOT = Path(__file__).resolve().parents[1]
CAPTURED_AT = datetime(2026, 7, 3, tzinfo=timezone.utc)


def _metric(
    name: str,
    value: str,
    year: int,
    *,
    period: str | None = None,
    currency: str = "CNY",
    unit: str = "CNY",
) -> MetricInput:
    return MetricInput(
        field_id=name,
        value=Decimal(value),
        period=period or f"FY{year}",
        currency=currency,
        unit=unit,
        source_url=f"https://example.com/{year}/{name}",
    )


def _annual(year: int, *, current: bool) -> AnnualLiquidityInput:
    values = (
        {
            "revenue": "100000000",
            "profit": "10000000",
            "current_assets": "50000000",
            "current_liabilities": "25000000",
            "inventory": "10000000",
            "capex": "-5000000",
            "operating_profit": "12000000",
            "interest": "-2000000",
            "liabilities": "60000000",
            "assets": "100000000",
            "ocf": "12000000",
        }
        if current
        else {
            "revenue": "80000000",
            "profit": "8000000",
            "current_assets": "40000000",
            "current_liabilities": "20000000",
            "inventory": "8000000",
            "capex": "4000000",
            "operating_profit": "9000000",
            "interest": "1000000",
            "liabilities": "50000000",
            "assets": "100000000",
            "ocf": "-1000000",
        }
    )
    period = FinancialPeriodRecord(
        report_date=date(year, 12, 31),
        period_type="annual",
        fiscal_year=year,
        revenue=Decimal(values["revenue"]),
        parent_net_profit=Decimal(values["profit"]),
        source_url=f"https://example.com/{year}/annual",
    )
    return AnnualLiquidityInput(
        financial_period=period,
        current_assets=_metric("current_assets", values["current_assets"], year),
        current_liabilities=_metric("current_liabilities", values["current_liabilities"], year),
        inventory=_metric("inventory", values["inventory"], year),
        capex_cash_paid=_metric("capex_cash_paid", values["capex"], year),
        operating_profit=_metric("operating_profit", values["operating_profit"], year),
        interest_expense=_metric("interest_expense", values["interest"], year),
        total_liabilities=_metric("total_liabilities", values["liabilities"], year),
        total_assets=_metric("total_assets", values["assets"], year),
        operating_cash_flow=_metric("operating_cash_flow", values["ocf"], year),
    )


def _latest(cash: str = "30000000", debt: str = "30000000") -> LatestBalanceSheetInput:
    return LatestBalanceSheetInput(
        period="2026Q1",
        monetary_funds=_metric("monetary_funds", cash, 2026, period="2026Q1"),
        short_term_borrowings=_metric("short_term_borrowings", debt, 2026, period="2026Q1"),
    )


def test_two_year_formulas_and_complete_derived_evidence():
    result = collect_financial_metrics(
        annual_records=[_annual(2024, current=False), _annual(2025, current=True)],
        latest_balance_sheet=_latest(),
        captured_at=CAPTURED_AT,
    )
    current, previous = result.periods
    assert current.current_ratio == Decimal("2.00")
    assert current.quick_ratio == Decimal("1.60")
    assert current.capex_cny_million == Decimal("5.00")
    assert current.capex_to_revenue == Decimal("0.05")
    assert current.interest_coverage == Decimal("7.00")
    assert current.debt_to_asset == Decimal("0.60")
    assert current.ocf_positive is True
    assert current.cash_flow_gt_noi is True
    assert previous.ocf_positive is False
    assert result.short_term_debt_pressure is False
    assert result.sustained_positive_ocf is False
    assert result.failures == []
    assert all(item.derived_evidence is not None for item in result.source_values)
    assert all(
        evidence.inputs and all(item.source_url for item in evidence.inputs)
        for evidence in (item.derived_evidence for item in result.source_values)
    )
    interest = next(
        item for item in result.source_values if item.field_id == "interest_coverage_current"
    )
    assert interest.derived_evidence.inputs[1].value == Decimal("-2000000")
    assert {item.period for item in interest.derived_evidence.inputs} == {"FY2025"}
    comment = next(item for item in result.source_values if item.field_id == "liquidity_comment")
    assert {item.field_id for item in comment.derived_evidence.inputs} >= {
        "current_ratio_current",
        "current_ratio_previous",
        "short_term_debt_pressure",
        "sustained_positive_ocf",
    }


def test_capex_sign_and_negative_input_rules():
    assert normalize_capex(Decimal("-5")) == Decimal("5")
    assert normalize_capex(Decimal("5")) == Decimal("5")
    with pytest.raises(ValueError, match="current assets"):
        calculate_current_ratio(Decimal("-1"), Decimal("2"))
    with pytest.raises(ValueError, match="inventory"):
        calculate_quick_ratio(Decimal("2"), Decimal("-1"), Decimal("2"))
    with pytest.raises(ValueError, match="total liabilities"):
        calculate_debt_to_asset(Decimal("-1"), Decimal("2"))
    assert calculate_interest_coverage(Decimal("12"), Decimal("-2")) == Decimal("7.00")


def test_zero_denominators_and_missing_inputs_become_explicit_failures():
    record = _annual(2025, current=True)
    record.current_liabilities.value = Decimal("0")
    record.interest_expense.value = Decimal("0")
    record.total_assets.value = Decimal("0")
    record.financial_period.revenue = Decimal("0")
    record.inventory = None
    _, values, failures = calculate_period_metrics(
        record, suffix="current", captured_at=CAPTURED_AT
    )
    failed = {item.field_id: item.reason for item in failures}
    assert "greater than zero" in failed["current_ratio_current"]
    assert "Missing inputs: inventory" == failed["quick_ratio_current"]
    assert "greater than zero" in failed["capex_to_revenue_current"]
    assert "greater than zero" in failed["interest_coverage_current"]
    assert "greater than zero" in failed["debt_to_asset_current"]
    assert not {item.field_id for item in values} & set(failed)


def test_metric_period_currency_and_unit_are_rejected_at_input_boundary():
    annual_data = _annual(2025, current=True).model_dump(mode="python")
    annual_data["current_assets"]["period"] = "FY2024"
    with pytest.raises(ValidationError, match="current_assets period FY2024 does not match FY2025"):
        AnnualLiquidityInput.model_validate(annual_data)

    metric_data = _metric("current_assets", "1", 2025).model_dump(mode="python")
    metric_data["currency"] = "USD"
    with pytest.raises(ValidationError, match="CNY"):
        MetricInput.model_validate(metric_data)
    metric_data["currency"] = "CNY"
    metric_data["unit"] = "CNY million"
    with pytest.raises(ValidationError, match="CNY"):
        MetricInput.model_validate(metric_data)

    latest_data = _latest().model_dump(mode="python")
    latest_data["short_term_borrowings"]["period"] = "FY2025"
    with pytest.raises(ValidationError, match="short_term_borrowings period FY2025"):
        LatestBalanceSheetInput.model_validate(latest_data)


def test_equal_cash_and_debt_is_not_pressure_and_one_negative_ocf_is_not_sustained():
    result = collect_financial_metrics(
        annual_records=[_annual(2025, current=True), _annual(2024, current=False)],
        latest_balance_sheet=_latest(cash="30000000", debt="30000000"),
        captured_at=CAPTURED_AT,
    )
    assert result.short_term_debt_pressure is False
    assert result.sustained_positive_ocf is False
    pressure = next(item for item in result.source_values if item.field_id == "short_term_debt_pressure")
    assert pressure.value == "No"
    net_income = next(item for item in result.source_values if item.field_id == "net_income_exceeds_ocf")
    assert net_income.value == "No"
    assert net_income.derived_evidence.formula == "parent_net_profit > operating_cash_flow"


def test_short_term_pressure_uses_latest_explicit_balance_sheet_period():
    result = collect_financial_metrics(
        annual_records=[_annual(2025, current=True), _annual(2024, current=False)],
        latest_balance_sheet=_latest(cash="20000000", debt="30000000"),
        captured_at=CAPTURED_AT,
    )
    pressure = next(item for item in result.source_values if item.field_id == "short_term_debt_pressure")
    assert result.short_term_debt_pressure is True
    assert pressure.period == "2026Q1"
    assert "monetary funds CNY 20.00 million" in pressure.value
    assert [item.field_id for item in pressure.derived_evidence.inputs] == [
        "monetary_funds",
        "short_term_borrowings",
    ]


def test_node_consumes_part_04_annual_records_by_fiscal_year():
    current = _annual(2025, current=True)
    previous = _annual(2024, current=False)
    state = {
        "created_at": CAPTURED_AT.isoformat(),
        "part_results": {
            "part_04": {
                "annual_records": [
                    current.financial_period.model_dump(mode="json"),
                    previous.financial_period.model_dump(mode="json"),
                ]
            },
            "part_06_input": {
                "annual_metrics": [
                    {"fiscal_year": 2025, **current.model_dump(mode="json", exclude={"financial_period"})},
                    {"fiscal_year": 2024, **previous.model_dump(mode="json", exclude={"financial_period"})},
                ],
                "latest_balance_sheet": _latest().model_dump(mode="json"),
            },
        },
        "source_values": [],
        "failed_fields": [],
    }
    output = financial_metrics_node(state)
    assert output["part_results"]["part_06"]["periods"][0]["fiscal_year"] == 2025
    assert any(item["field_id"] == "current_ratio_current" for item in output["source_values"])


def test_part_06_mapping_is_valid_for_real_template():
    fragment = load_yaml(ROOT / "configs" / "fields" / "part_06.yaml")
    mappings = TypeAdapter(list[FieldMapping]).validate_python(fragment["fields"])
    validate_template_mapping(
        ROOT / "Workup_template_260617-外测版.docx",
        [item.model_dump(mode="json") for item in mappings],
    )
