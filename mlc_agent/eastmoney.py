from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from mlc_agent.company_resolver import build_eastmoney_url
from mlc_agent.exceptions import DataSourceError
from mlc_agent.schemas import CompanyIdentity, SourceValue


DATA_API = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
PROFILE_REPORT = "RPT_F10_BASIC_ORGINFO"
FINANCE_REPORT = "RPT_F10_FINANCE_MAINFINADATA"
MARKET_CLOSE_REPORT = "RPT_STOCK_HISTORYMARK"


def _get_data_records(
    client: httpx.Client,
    report_name: str,
    company: CompanyIdentity,
    *,
    page_size: int,
    sort: bool = False,
) -> list[dict[str, Any]]:
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": f'(SECUCODE="{company.eastmoney_secu_code}")',
        "pageNumber": "1",
        "pageSize": str(page_size),
    }
    if sort:
        params.update({"sortTypes": "-1", "sortColumns": "REPORT_DATE"})
    response = client.get(DATA_API, params=params)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise DataSourceError(f"东方财富接口失败: {payload.get('message', 'unknown error')}")
    return payload.get("result", {}).get("data") or []


def fetch_company_profile(client: httpx.Client, company: CompanyIdentity) -> dict[str, Any]:
    records = _get_data_records(client, PROFILE_REPORT, company, page_size=1)
    if len(records) != 1:
        raise DataSourceError("东方财富未返回唯一的公司资料。")
    return records[0]


def fetch_financial_summary(client: httpx.Client, company: CompanyIdentity) -> dict[str, Any]:
    records = _get_data_records(client, FINANCE_REPORT, company, page_size=20, sort=True)
    if not records:
        raise DataSourceError("东方财富未返回财务摘要。")
    latest = records[0]
    annual = next(
        (
            record
            for record in records
            if record.get("REPORT_TYPE") == "年报"
            or str(record.get("REPORT_DATE", "")).startswith(f"{record.get('REPORT_YEAR')}-12-31")
        ),
        None,
    )
    if annual is None:
        raise DataSourceError("东方财富未返回最近完整年度财务数据。")
    return {"latest": latest, "annual": annual}


def fetch_financial_period_records(
    client: httpx.Client,
    company: CompanyIdentity,
    *,
    page_size: int = 40,
) -> list[dict[str, Any]]:
    """Return ordered Eastmoney financial periods without interpreting YTD values.

    Period classification and single-quarter restoration belong to the operating
    performance business module.  Keeping this function source-only prevents
    downstream consumers from accidentally treating cumulative figures as
    standalone quarters.
    """
    records = _get_data_records(
        client,
        FINANCE_REPORT,
        company,
        page_size=page_size,
        sort=True,
    )
    if not records:
        raise DataSourceError("东方财富未返回标准财务期间数据。")
    return records


def fetch_market_snapshot(
    client: httpx.Client,
    company: CompanyIdentity,
    total_shares: float | int | None,
) -> dict[str, Any]:
    try:
        response = client.get(
            KLINE_API,
            params={
                "secid": company.eastmoney_secid,
                "klt": "101",
                "fqt": "0",
                "lmt": "300",
                "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
            headers={"Referer": build_eastmoney_url(company)},
        )
        response.raise_for_status()
        payload = response.json()
        klines = payload.get("data", {}).get("klines") or []
        if not klines:
            raise DataSourceError("东方财富未返回日 K 线行情。")
    except (httpx.HTTPError, ValueError, DataSourceError):
        return _fetch_latest_close_snapshot(client, company, total_shares)

    parsed: list[dict[str, Any]] = []
    for line in klines:
        columns = str(line).split(",")
        if len(columns) < 5:
            continue
        parsed.append(
            {
                "date": datetime.strptime(columns[0], "%Y-%m-%d").date(),
                "close": float(columns[2]),
                "high": float(columns[3]),
                "low": float(columns[4]),
            }
        )
    if not parsed:
        raise DataSourceError("东方财富日 K 线格式无效。")
    latest = parsed[-1]
    start_date = latest["date"] - timedelta(days=365)
    trailing_year = [record for record in parsed if record["date"] >= start_date]
    market_cap = latest["close"] * float(total_shares) if total_shares else None
    return {
        "quote_date": latest["date"].isoformat(),
        "current": latest["close"],
        "high52w": max(record["high"] for record in trailing_year),
        "low52w": min(record["low"] for record in trailing_year),
        "market_capital": market_cap,
        "total_shares": total_shares,
        "price_metric": "kline.latest.close",
    }


def _fetch_latest_close_snapshot(
    client: httpx.Client,
    company: CompanyIdentity,
    total_shares: float | int | None,
) -> dict[str, Any]:
    response = client.get(
        DATA_API,
        params={
            "reportName": MARKET_CLOSE_REPORT,
            "columns": "ALL",
            "filter": f'(SECUCODE="{company.eastmoney_secu_code}")',
            "pageNumber": "1",
            "pageSize": "1",
            "sortTypes": "-1",
            "sortColumns": "DIAGNOSE_DATE",
        },
    )
    response.raise_for_status()
    payload = response.json()
    records = payload.get("result", {}).get("data") or []
    if not payload.get("success") or len(records) != 1:
        raise DataSourceError("东方财富未返回最新收盘价。")
    record = records[0]
    close = record.get("CLOSE")
    quote_date = str(record.get("DIAGNOSE_DATE") or "").split(" ", 1)[0]
    if close is None or not quote_date:
        raise DataSourceError("东方财富最新收盘价记录缺少价格或日期。")
    market_cap = float(close) * float(total_shares) if total_shares else None
    return {
        "quote_date": quote_date,
        "current": float(close),
        "high52w": None,
        "low52w": None,
        "market_capital": market_cap,
        "total_shares": total_shares,
        "price_metric": f"{MARKET_CLOSE_REPORT}.CLOSE",
    }


def _report_period(record: dict[str, Any]) -> str:
    return str(record.get("REPORT_DATE_NAME") or record.get("REPORT_DATE") or "")


def _report_date(record: dict[str, Any]) -> datetime | None:
    raw = str(record.get("REPORT_DATE") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _balance_sheet_period(record: dict[str, Any]) -> str:
    report_date = _report_date(record)
    if report_date:
        return f"as of {report_date.strftime('%d %b %Y')}"
    return _report_period(record)


def _financial_year_period(record: dict[str, Any]) -> str:
    year = str(record.get("REPORT_YEAR") or "").strip()
    report_date = _report_date(record)
    if not year and report_date:
        year = str(report_date.year)
    return f"FY{year}" if year else _report_period(record)


def _english_exchange(company: CompanyIdentity) -> str:
    suffix = company.eastmoney_secu_code.rsplit(".", 1)[-1].upper()
    names = {
        "SZ": "Shenzhen Stock Exchange",
        "SH": "Shanghai Stock Exchange",
        "BJ": "Beijing Stock Exchange",
    }
    return names.get(suffix, company.exchange)


def _format_cny(value: float | int, period: str | None = None) -> str:
    amount = float(value)
    if abs(amount) >= 1_000_000_000:
        formatted = f"CNY {amount / 1_000_000_000:.2f}bn"
    elif abs(amount) >= 1_000_000:
        formatted = f"CNY {amount / 1_000_000:.2f}m"
    else:
        formatted = f"CNY {amount:,.2f}"
    return f"{formatted} ({period})" if period else formatted


def normalize_eastmoney_values(
    company: CompanyIdentity,
    profile: dict[str, Any] | None,
    financials: dict[str, Any] | None,
    market: dict[str, Any] | None,
    captured_at: datetime,
) -> list[SourceValue]:
    profile_url = build_eastmoney_url(company)
    values: list[SourceValue] = []

    def add(
        field_id: str,
        value: str | None,
        raw_value: Any,
        *,
        period: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if value is None or value == "":
            return
        values.append(
            SourceValue(
                field_id=field_id,
                value=value,
                raw_value=raw_value,
                source="eastmoney",
                source_url=profile_url,
                captured_at=captured_at,
                period=period,
                metadata=metadata or {},
            )
        )

    if profile:
        add("company_english_name", profile.get("ORG_NAME_EN"), profile.get("ORG_NAME_EN"))
        add("listed_exchange", _english_exchange(company), profile.get("TRADE_MARKET"))
        website = profile.get("ORG_WEB")
        if website:
            website = str(website).strip()
            if not website.startswith(("http://", "https://")):
                website = f"https://{website}"
        add("official_website", website, profile.get("ORG_WEB"))

    if financials:
        latest = financials["latest"]
        annual = financials["annual"]
        latest_period = _balance_sheet_period(latest)
        annual_period = _financial_year_period(annual)
        financial_fields = [
            ("total_assets", latest.get("TOTAL_ASSETS_PK"), latest_period, "TOTAL_ASSETS_PK"),
            ("total_equity", latest.get("TOTAL_EQUITY_PK"), latest_period, "TOTAL_EQUITY_PK"),
            ("annual_revenue", annual.get("TOTALOPERATEREVE"), annual_period, "TOTALOPERATEREVE"),
        ]
        for field_id, raw, period, metric in financial_fields:
            if raw is not None:
                add(
                    field_id,
                    _format_cny(raw, period),
                    raw,
                    period=period,
                    metadata={"metric": metric, "currency": annual.get("CURRENCY") or "CNY"},
                )
        parent_profit = annual.get("PARENTNETPROFIT")
        if parent_profit is not None:
            add(
                "annual_net_profit",
                _format_cny(parent_profit, f"{annual_period}, attributable to owners"),
                parent_profit,
                period=annual_period,
                metadata={
                    "metric": "PARENTNETPROFIT",
                    "currency": annual.get("CURRENCY") or "CNY",
                    "accounting_basis": "attributable_to_owners_of_parent",
                },
            )

    if market:
        market_fields = [
            (
                "current_price",
                market.get("current"),
                market.get("price_metric") or "kline.latest.close",
            ),
            ("week_52_high", market.get("high52w"), "kline.trailing_365d.high"),
            ("week_52_low", market.get("low52w"), "kline.trailing_365d.low"),
        ]
        for field_id, raw, metric in market_fields:
            if raw not in (None, "-"):
                add(
                    field_id,
                    f"CNY {float(raw):.2f}",
                    raw,
                    period=market.get("quote_date"),
                    metadata={"metric": metric, "quote_date": market.get("quote_date")},
                )
        raw_market_cap = market.get("market_capital")
        if raw_market_cap not in (None, "-"):
            add(
                "market_capitalization",
                _format_cny(raw_market_cap),
                raw_market_cap,
                period=market.get("quote_date"),
                metadata={
                    "metric": "market.latest.close * finance.latest.total_shares",
                    "currency": "CNY",
                    "quote_date": market.get("quote_date"),
                    "total_shares": market.get("total_shares"),
                },
            )
    return values
