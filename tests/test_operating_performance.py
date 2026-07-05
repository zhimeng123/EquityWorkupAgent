from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from mlc_agent.eastmoney import fetch_financial_period_records
from mlc_agent.operating_performance import (
    FinancialPeriodRecord,
    ReportDocument,
    collect_operating_performance,
    build_outlook_english_prompt,
    convert_to_cny_million,
    extract_customers_suppliers,
    extract_revenue_breakdown,
    normalize_financial_periods,
    operating_performance_node,
    restore_standalone_quarters,
    select_operating_periods,
)
from mlc_agent.report_parser import ParsedReport, PdfPage, PdfTable
from mlc_agent.schemas import CompanyIdentity


COMPANY = CompanyIdentity(
    company_name="紫光股份",
    company_short_name="紫光股份",
    stock_code="000938",
    exchange="深圳证券交易所",
    eastmoney_secid="0.000938",
    eastmoney_secu_code="000938.SZ",
    xueqiu_symbol="SZ000938",
)
URL = "https://quote.eastmoney.com/sz000938.html"


def _raw(report_date, revenue, profit, gross_profit):
    return {
        "REPORT_DATE": f"{report_date} 00:00:00",
        "TOTALOPERATEREVE": revenue,
        "PARENTNETPROFIT": profit,
        "GROSSPROFIT": gross_profit,
        "CURRENCY": "CNY",
        "UNIT": "CNY",
    }


def _record(year, period_type, revenue):
    endings = {
        "q1_ytd": (3, 31),
        "half_year_ytd": (6, 30),
        "q3_ytd": (9, 30),
        "annual": (12, 31),
    }
    month, day = endings[period_type]
    return FinancialPeriodRecord(
        report_date=f"{year}-{month:02d}-{day:02d}",
        period_type=period_type,
        fiscal_year=year,
        revenue=Decimal(revenue),
        parent_net_profit=Decimal(revenue) / 10,
        gross_profit=Decimal(revenue) / 4,
        source_url=URL,
    )


def _report(year=2025, kind="annual_report"):
    return ReportDocument(
        report=ParsedReport(
            path=f"/{year}.pdf",
            pages=[
                PdfPage(
                    page_number=10,
                    text="单位：人民币万元\n未来发展战略将聚焦核心业务。\n主要风险为市场需求不确定性。",
                    tables=[
                        PdfTable(rows=[["分产品", "营业收入"], ["ICT基础设施", "12,500"]]),
                        PdfTable(
                            rows=[
                                ["主要客户", "销售额", "占比"],
                                ["Customer 1", "2,000", "12.5%"],
                            ]
                        ),
                        PdfTable(
                            rows=[
                                ["主要供应商", "采购额", "占比"],
                                ["Supplier 1", "1,000", "8.0%"],
                            ]
                        ),
                    ],
                )
            ],
        ),
        source_url=f"https://example.com/{year}.pdf",
        report_year=year,
        kind=kind,
    )


def test_eastmoney_period_fetch_returns_source_records_without_interpretation():
    records = [_raw("2025-12-31", 400, 40, 100)]

    def handler(request):
        assert request.url.params["reportName"] == "RPT_F10_FINANCE_MAINFINADATA"
        assert request.url.params["sortColumns"] == "REPORT_DATE"
        return httpx.Response(200, json={"success": True, "result": {"data": records}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_financial_period_records(client, COMPANY) == records


def test_normalize_and_select_two_annual_periods_plus_latest_interim():
    records = normalize_financial_periods(
        [
            _raw("2026-06-30", 250, 25, 62.5),
            _raw("2025-12-31", 400, 40, 100),
            _raw("2024-12-31", 300, 30, 75),
            _raw("2023-12-31", 200, 20, 50),
        ],
        source_url=URL,
    )
    annual, interim = select_operating_periods(records)
    assert [item.fiscal_year for item in annual] == [2025, 2024]
    assert interim is not None and interim.report_date.isoformat() == "2026-06-30"
    assert all(item.currency == "CNY" and item.unit == "CNY" for item in records)


def test_restore_standalone_quarters_uses_adjacent_ytd_values_and_numeric_margin():
    records = [
        _record(2025, "q1_ytd", "100"),
        _record(2025, "half_year_ytd", "230"),
        _record(2025, "q3_ytd", "390"),
        _record(2025, "annual", "600"),
    ]
    quarters, errors = restore_standalone_quarters(records)
    by_quarter = {item.fiscal_quarter: item for item in quarters}
    assert errors == []
    assert by_quarter[1].revenue == Decimal("100")
    assert by_quarter[2].revenue == Decimal("130")
    assert by_quarter[3].revenue == Decimal("160")
    assert by_quarter[4].revenue == Decimal("210")
    assert by_quarter[4].gross_margin == Decimal("25")
    assert by_quarter[2].formulas["revenue"] == "half_year_ytd.revenue - q1_ytd.revenue"


def test_missing_adjacent_ytd_period_does_not_create_false_quarter():
    quarters, errors = restore_standalone_quarters(
        [_record(2025, "q1_ytd", "100"), _record(2025, "q3_ytd", "390")]
    )
    assert [item.fiscal_quarter for item in quarters] == [1]
    assert errors == ["FY2025 Q3: missing adjacent half_year_ytd"]


def test_latest_eight_quarter_window_does_not_fill_current_gaps_with_old_periods():
    quarters, errors = restore_standalone_quarters(
        [
            _record(2025, "q1_ytd", "100"),
            _record(2025, "q3_ytd", "390"),
            _record(2016, "q1_ytd", "10"),
            _record(2016, "half_year_ytd", "30"),
        ]
    )

    assert [(item.fiscal_year, item.fiscal_quarter) for item in quarters] == [(2025, 1)]
    assert errors == ["FY2025 Q3: missing adjacent half_year_ytd"]


@pytest.mark.parametrize(
    ("unit", "expected"),
    [("元", "1"), ("千元", "1"), ("万元", "1"), ("百万元", "1"), ("亿元", "100")],
)
def test_cny_unit_conversion_is_explicit(unit, expected):
    source_values = {"元": "1000000", "千元": "1000", "万元": "100", "百万元": "1", "亿元": "1"}
    result, formula = convert_to_cny_million(source_values[unit], unit)
    assert result == Decimal(expected)
    assert "CNY million" in formula


def test_anonymous_customer_and_supplier_names_are_preserved_exactly():
    items = extract_customers_suppliers(_report())
    assert [(item.kind, item.disclosed_name) for item in items] == [
        ("customer", "Customer 1"),
        ("supplier", "Supplier 1"),
    ]


def test_multirow_annual_revenue_table_extracts_business_and_geography_for_both_years():
    document = ReportDocument(
        report=ParsedReport(
            path="/2025.pdf",
            pages=[PdfPage(
                page_number=20,
                text="营业收入构成\n单位：元",
                tables=[PdfTable(rows=[
                    ["", "2025年", None, "2024年", None, ""],
                    ["", None, None, None, None, "同比增减"],
                    [None, "金额", "占营业收入比重", "金额", "占营业收入比重", None],
                    ["分产品", None, None, None, None, None],
                    ["ICT基础设施与服务", "76,846,762,924.64", "79.43%", "54,459,096,509.28", "68.91%", "41.11%"],
                    ["分地区", None, None, None, None, None],
                    ["国内", "92,037,811,596.19", "95.13%", "75,957,053,287.87", "96.12%", "21.17%"],
                    ["分销售模式", None, None, None, None, None],
                    ["经销", "48,181,829,542.40", "49.80%", "47,131,366,154.85", "59.64%", "2.23%"],
                ])],
            )],
        ),
        source_url="https://example.com/2025.pdf",
        report_year=2025,
        kind="annual_report",
    )

    items = extract_revenue_breakdown([document])

    assert {(item.report_year, item.category, item.name) for item in items} == {
        (2025, "business_segment", "ICT基础设施与服务"),
        (2024, "business_segment", "ICT基础设施与服务"),
        (2025, "geography", "国内"),
        (2024, "geography", "国内"),
    }
    assert next(item for item in items if item.report_year == 2025 and item.name == "国内").revenue_cny_million == Decimal("92037.81159619")


def test_collection_exposes_reusable_records_and_evidence_backed_text_only():
    raw = [
        _raw("2025-03-31", 100_000_000, 10_000_000, 25_000_000),
        _raw("2025-06-30", 230_000_000, 23_000_000, 57_500_000),
        _raw("2025-09-30", 390_000_000, 39_000_000, 97_500_000),
        _raw("2025-12-31", 600_000_000, 60_000_000, 150_000_000),
        _raw("2024-12-31", 500_000_000, 50_000_000, 125_000_000),
    ]
    rendered_items = []

    def render_english(items):
        rendered_items.extend(items)
        return "Outlook: The company will focus on its core businesses.\nRisk: Market demand remains uncertain."

    result = collect_operating_performance(
        company=COMPANY,
        raw_financial_records=raw,
        report_documents=[_report(2025), _report(2024)],
        captured_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        outlook_english_renderer=render_english,
    )
    by_id = {item.field_id: item for item in result.source_values}
    assert len(result.annual_records) == 2
    assert len(result.quarterly_records) == 4
    assert set(by_id) == {
        "revenue_breakdown",
        "quarterly_revenue_profitability",
        "customers_suppliers_concentration",
        "business_outlook_risks",
    }
    assert "FY2025 Q4: revenue CNY 210.00 million" in by_id["quarterly_revenue_profitability"].value
    assert "Customer 1" in by_id["customers_suppliers_concentration"].value
    assert "The company will focus on its core businesses" in by_id["business_outlook_risks"].value
    assert rendered_items and all(item.source_url for item in rendered_items)
    assert "未来发展战略将聚焦核心业务" in str(by_id["business_outlook_risks"].raw_value)
    assert "未来发展战略将聚焦核心业务" in str(
        by_id["business_outlook_risks"].item_evidence[0].raw_value
    )
    assert by_id["business_outlook_risks"].item_evidence[0].source_url == "https://example.com/2025.pdf"
    assert by_id["revenue_breakdown"].item_evidence


def test_non_cny_financial_record_is_rejected_instead_of_silent_conversion():
    raw = _raw("2025-12-31", 100, 10, 25)
    raw["CURRENCY"] = "USD"
    with pytest.raises(ValueError, match="unsupported financial record currency"):
        normalize_financial_periods([raw], source_url=URL)


def test_financial_record_unit_is_normalized_to_cny_before_quarter_math():
    raw = _raw("2025-12-31", 100, 10, 25)
    raw["UNIT"] = "万元"
    record = normalize_financial_periods([raw], source_url=URL)[0]
    assert record.unit == "CNY"
    assert record.revenue == Decimal("1000000")
    assert record.parent_net_profit == Decimal("100000")


def test_missing_english_renderer_records_real_failure_without_chinese_source_value():
    result = collect_operating_performance(
        company=COMPANY,
        raw_financial_records=[_raw("2025-12-31", 100, 10, 25)],
        report_documents=[_report(2025)],
        captured_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )
    assert "business_outlook_risks" not in {item.field_id for item in result.source_values}
    assert result.errors == [
        "FY2025 Q4: missing adjacent q3_ytd",
        "business_outlook_risks: verified disclosures found but English renderer was not provided",
    ]


def test_outlook_prompt_prohibits_new_facts_and_preserves_proper_names():
    item = _report().report.pages[0]
    prompt = build_outlook_english_prompt(
        tuple(
            extract
            for extract in collect_operating_performance(
                company=COMPANY,
                raw_financial_records=[_raw("2025-12-31", 100, 10, 25)],
                report_documents=[_report(2025)],
                captured_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
            ).outlook_risk_items
        )
    )
    assert item.text.splitlines()[1] in prompt
    assert "Do not add, infer, combine, omit, or soften any fact" in prompt
    assert "Preserve Chinese proper names exactly" in prompt


def test_node_injects_renderer_outside_serializable_state():
    report = _report(2025)
    state = {
        "company": COMPANY.model_dump(mode="json"),
        "created_at": "2026-07-03T00:00:00+00:00",
        "source_values": [],
        "node_errors": [],
        "part_results": {
            "part_04_input": {
                "raw_financial_records": [_raw("2025-12-31", 100, 10, 25)],
                "report_documents": [report.model_dump(mode="json")],
            }
        },
    }
    assert "outlook_english_renderer" not in state["part_results"]["part_04_input"]

    result = operating_performance_node(
        state,
        outlook_english_renderer=lambda items: "Outlook: Focus remains on core businesses.",
    )

    values = {item["field_id"]: item for item in result["source_values"]}
    assert values["business_outlook_risks"]["value"] == (
        "\nOutlook: Focus remains on core businesses."
    )
    assert "outlook_english_renderer" not in result["part_results"]["part_04"]
