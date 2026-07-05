from datetime import datetime, timezone

import httpx

from mlc_agent.eastmoney import (
    fetch_financial_summary,
    fetch_market_snapshot,
    normalize_eastmoney_values,
)
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


def test_financial_summary_selects_latest_period_and_latest_full_year():
    records = [
        {"REPORT_DATE": "2026-03-31 00:00:00", "REPORT_DATE_NAME": "2026一季报", "REPORT_TYPE": "一季报", "REPORT_YEAR": "2026"},
        {"REPORT_DATE": "2025-12-31 00:00:00", "REPORT_DATE_NAME": "2025年报", "REPORT_TYPE": "年报", "REPORT_YEAR": "2025"},
        {"REPORT_DATE": "2025-09-30 00:00:00", "REPORT_DATE_NAME": "2025三季报", "REPORT_TYPE": "三季报", "REPORT_YEAR": "2025"},
    ]

    def handler(request):
        return httpx.Response(
            200,
            json={"success": True, "result": {"data": records}},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_financial_summary(client, COMPANY)
    assert result["latest"]["REPORT_DATE_NAME"] == "2026一季报"
    assert result["annual"]["REPORT_DATE_NAME"] == "2025年报"


def test_market_snapshot_uses_latest_close_and_trailing_365_days():
    lines = [
        "2025-01-01,8.00,8.50,50.00,1.00,100,1000,0,0,0,0",
        "2025-07-02,10.00,10.50,11.00,9.00,100,1000,0,0,0,0",
        "2026-07-02,12.00,12.50,13.00,8.00,100,1000,0,0,0,0",
    ]

    def handler(request):
        assert request.headers["referer"] == "https://quote.eastmoney.com/sz000938.html"
        assert request.url.params["fqt"] == "0"
        return httpx.Response(200, json={"data": {"klines": lines}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_market_snapshot(client, COMPANY, 1000)
    assert result["current"] == 12.5
    assert result["high52w"] == 13.0
    assert result["low52w"] == 8.0
    assert result["market_capital"] == 12_500


def test_market_snapshot_uses_eastmoney_close_report_when_kline_disconnects():
    def handler(request):
        if request.url.host == "push2his.eastmoney.com":
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.", request=request
            )
        assert request.url.params["reportName"] == "RPT_STOCK_HISTORYMARK"
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "data": [
                        {
                            "DIAGNOSE_DATE": "2026-07-02 00:00:00",
                            "CLOSE": 29.03,
                        }
                    ]
                },
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_market_snapshot(client, COMPANY, 2_860_000_000)

    assert result["quote_date"] == "2026-07-02"
    assert result["current"] == 29.03
    assert result["market_capital"] == 29.03 * 2_860_000_000
    assert result["high52w"] is None
    assert result["low52w"] is None
    assert result["price_metric"] == "RPT_STOCK_HISTORYMARK.CLOSE"


def test_normalized_financial_values_use_english_periods_and_parent_profit_basis():
    profile = {"TRADE_MARKET": "深圳证券交易所"}
    financials = {
        "latest": {
            "REPORT_DATE": "2026-03-31 00:00:00",
            "REPORT_DATE_NAME": "2026一季报",
            "REPORT_YEAR": "2026",
            "TOTAL_ASSETS_PK": 103_000_000_000,
            "TOTAL_EQUITY_PK": 18_000_000_000,
            "CURRENCY": "CNY",
        },
        "annual": {
            "REPORT_DATE": "2025-12-31 00:00:00",
            "REPORT_DATE_NAME": "2025年报",
            "REPORT_YEAR": "2025",
            "TOTALOPERATEREVE": 96_000_000_000,
            "PARENTNETPROFIT": 1_680_000_000,
            "CURRENCY": "CNY",
        },
    }

    values = normalize_eastmoney_values(
        COMPANY,
        profile,
        financials,
        None,
        datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    by_id = {item.field_id: item for item in values}

    assert by_id["listed_exchange"].value == "Shenzhen Stock Exchange"
    assert by_id["total_assets"].value == "CNY 103.00bn (as of 31 Mar 2026)"
    assert by_id["annual_revenue"].value == "CNY 96.00bn (FY2025)"
    assert by_id["annual_net_profit"].value == (
        "CNY 1.68bn (FY2025, attributable to owners)"
    )
    assert by_id["annual_net_profit"].metadata["accounting_basis"] == (
        "attributable_to_owners_of_parent"
    )
