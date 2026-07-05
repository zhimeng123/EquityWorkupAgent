from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mlc_agent.deep_financial_analysis import (
    AnnualBalanceSheetDetails,
    ImpairmentComponent,
    analyze_intangibles,
    analyze_receivable_impairment,
    collect_deep_financial_analysis,
    compare_receivables_and_revenue_growth,
)
from mlc_agent.operating_performance import FinancialPeriodRecord


def _financial(year, revenue, profit="100"):
    return FinancialPeriodRecord(
        report_date=f"{year}-12-31",
        period_type="annual",
        fiscal_year=year,
        revenue=Decimal(revenue),
        parent_net_profit=Decimal(profit),
        source_url=f"https://example.com/finance-{year}",
    )


def _balance(
    year,
    *,
    receivable="100",
    gross="120",
    goodwill="10",
    intangibles="15",
    assets="100",
    scope="group-a",
):
    return AnnualBalanceSheetDetails(
        fiscal_year=year,
        report_date=f"{year}-12-31",
        accounts_receivable=Decimal(receivable),
        accounts_receivable_gross=Decimal(gross),
        goodwill=Decimal(goodwill),
        intangible_assets=Decimal(intangibles),
        total_assets=Decimal(assets),
        consolidation_scope_id=scope,
        source_url=f"https://example.com/report-{year}.pdf",
        page_number=88,
    )


def _component(
    component_id,
    category,
    amount,
    *,
    material=False,
    one_time=False,
    year=2025,
    source="annual_report",
    source_url="https://example.com/report-2025.pdf",
    evidence_date="2026-03-31",
):
    return ImpairmentComponent(
        component_id=component_id,
        name=component_id,
        category=category,
        amount=Decimal(amount),
        fiscal_year=year,
        officially_material=material,
        officially_one_time=one_time,
        source=source,
        source_url=source_url,
        evidence_date=evidence_date,
        page_number=120,
    )


def test_receivables_growth_uses_only_accounts_receivable_and_comparable_revenue():
    result = compare_receivables_and_revenue_growth(
        _financial(2025, "120"),
        _financial(2024, "100"),
        _balance(2025, receivable="150", gross="160"),
        _balance(2024, receivable="100"),
    )
    assert result.receivables_growth == Decimal("0.5")
    assert result.revenue_growth == Decimal("0.2")
    assert result.receivables_growth_higher is True


def test_receivables_growth_rejects_zero_previous_basis():
    with pytest.raises(ValueError, match="accounts receivable previous-year value is zero"):
        compare_receivables_and_revenue_growth(
            _financial(2025, "120"),
            _financial(2024, "100"),
            _balance(2025),
            _balance(2024, receivable="0"),
        )


def test_receivables_growth_rejects_period_and_scope_mismatch():
    with pytest.raises(ValueError, match="consolidation scopes"):
        compare_receivables_and_revenue_growth(
            _financial(2025, "120"),
            _financial(2024, "100"),
            _balance(2025, scope="current-group"),
            _balance(2024, scope="previous-group"),
        )
    with pytest.raises(ValueError, match="periods do not match"):
        compare_receivables_and_revenue_growth(
            _financial(2025, "120"),
            _financial(2024, "100"),
            _balance(2024),
            _balance(2023),
        )


def test_impairment_filters_other_assets_and_uses_gross_receivables_denominator():
    result = analyze_receivable_impairment(
        [
            _component("bad-debt", "accounts_receivable_bad_debt", "6"),
            _component("credit", "receivable_credit_impairment", "4"),
            _component("contract", "contract_asset_impairment", "2"),
            _component("inventory", "other_asset_impairment", "999"),
        ],
        _financial(2025, "1000", profit="100"),
        _balance(2025, gross="120"),
    )
    assert [item.component_id for item in result.included_components] == [
        "bad-debt",
        "credit",
        "contract",
    ]
    assert result.total_impairment == Decimal("12")
    assert result.impairment_to_revenue == Decimal("0.012")
    assert result.impairment_to_gross_receivables == Decimal("0.1")
    assert result.material_one_time_impairment is True


def test_material_impairment_quantitative_boundary_is_inclusive():
    result = analyze_receivable_impairment(
        [_component("bad-debt", "accounts_receivable_bad_debt", "10")],
        _financial(2025, "1000", profit="100"),
        _balance(2025),
    )
    assert result.material_one_time_impairment is True
    assert result.material_basis.startswith("quantitative")


def test_nonpositive_profit_requires_formal_qualitative_designation():
    not_material = analyze_receivable_impairment(
        [_component("bad-debt", "accounts_receivable_bad_debt", "1000")],
        _financial(2025, "1000", profit="-1"),
        _balance(2025),
    )
    assert not_material.material_one_time_impairment is False
    formal = analyze_receivable_impairment(
        [_component("bad-debt", "accounts_receivable_bad_debt", "1", one_time=True)],
        _financial(2025, "1000", profit="0"),
        _balance(2025),
    )
    assert formal.material_one_time_impairment is True
    assert formal.material_basis.startswith("qualitative")


def test_intangibles_exactly_25_percent_does_not_exceed_threshold():
    boundary = analyze_intangibles(_balance(2025, goodwill="10", intangibles="15", assets="100"))
    above = analyze_intangibles(_balance(2025, goodwill="10", intangibles="15.01", assets="100"))
    assert boundary.ratio == Decimal("0.25")
    assert boundary.exceeds_25_percent is False
    assert above.exceeds_25_percent is True


def test_all_successful_fields_include_complete_formula_inputs_periods_and_urls():
    result = collect_deep_financial_analysis(
        annual_records=[_financial(2025, "1200", "100"), _financial(2024, "1000", "80")],
        balance_sheet_details=[
            _balance(2025, receivable="150", gross="160", goodwill="30", intangibles="10"),
            _balance(2024, receivable="100", gross="110"),
        ],
        impairment_components=[
            _component("bad-debt", "accounts_receivable_bad_debt", "6"),
            _component("contract", "contract_asset_impairment", "4"),
        ],
        captured_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )
    assert result.errors == []
    by_id = {item.field_id: item for item in result.source_values}
    assert set(by_id) == {
        "receivables_vs_revenue_growth",
        "receivable_impairment_analysis",
        "intangibles_asset_analysis",
    }
    for value in by_id.values():
        assert value.derived_evidence is not None
        assert value.derived_evidence.formula
        assert value.derived_evidence.inputs
        assert all(item.field_id and item.period and item.source_url for item in value.derived_evidence.inputs)
    impairment = by_id["receivable_impairment_analysis"]
    assert len(impairment.item_evidence) == 2
    assert all(item.source_url and "page 120" in item.period for item in impairment.item_evidence)
    assert by_id["receivables_vs_revenue_growth"].value.startswith("Yes")
    assert by_id["intangibles_asset_analysis"].value.startswith("Yes")


def test_collect_records_each_field_failure_without_fabricating_source_value():
    result = collect_deep_financial_analysis(
        annual_records=[_financial(2025, "1200"), _financial(2024, "1000")],
        balance_sheet_details=[_balance(2025), _balance(2024, receivable="0")],
        impairment_components=[],
        captured_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )
    assert {item.field_id for item in result.source_values} == {"intangibles_asset_analysis"}
    assert len(result.errors) == 2
    assert result.errors[0].startswith("receivables_vs_revenue_growth")
    assert result.errors[1].startswith("receivable_impairment_analysis")


def test_missing_comparative_receivable_names_the_required_fiscal_year():
    result = collect_deep_financial_analysis(
        annual_records=[_financial(2025, "1200"), _financial(2024, "1000")],
        balance_sheet_details=[_balance(2025)],
        impairment_components=[],
        captured_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )
    assert result.errors[0] == (
        "receivables_vs_revenue_growth: missing annual accounts receivable for FY2024"
    )


def test_cninfo_impairment_component_preserves_announcement_source_and_date():
    announcement_url = "https://static.cninfo.com.cn/finalpage/2026-02-10/impairment.pdf"
    result = collect_deep_financial_analysis(
        annual_records=[_financial(2025, "1000", "100"), _financial(2024, "900", "90")],
        balance_sheet_details=[_balance(2025), _balance(2024)],
        impairment_components=[
            _component(
                "announcement-bad-debt",
                "accounts_receivable_bad_debt",
                "12",
                source="cninfo",
                source_url=announcement_url,
                evidence_date="2026-02-10",
            )
        ],
        captured_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )
    value = next(
        item for item in result.source_values if item.field_id == "receivable_impairment_analysis"
    )
    item = value.item_evidence[0]
    assert item.source == "cninfo"
    assert item.source_url == announcement_url
    assert item.period == "FY2025; evidence date 2026-02-10, page 120"
    component_input = next(
        evidence
        for evidence in value.derived_evidence.inputs
        if evidence.field_id == "impairment_component:announcement-bad-debt"
    )
    assert component_input.source_url == announcement_url
    assert component_input.period == item.period
