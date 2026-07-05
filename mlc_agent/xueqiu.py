from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from mlc_agent.company_resolver import build_xueqiu_url
from mlc_agent.exceptions import DataSourceError
from mlc_agent.schemas import CompanyIdentity, SourceValue


QUOTE_API = "https://stock.xueqiu.com/v5/stock/quote.json"


def fetch_xueqiu_snapshot(client: httpx.Client, company: CompanyIdentity) -> dict[str, Any]:
    response = client.get(
        QUOTE_API,
        params={"symbol": company.xueqiu_symbol, "extend": "detail"},
        headers={"Referer": build_xueqiu_url(company)},
    )
    if response.status_code != 200:
        detail = response.text.strip().replace("\n", " ")[:160]
        raise DataSourceError(f"雪球行情接口 HTTP {response.status_code}: {detail}")
    payload = response.json()
    quote = payload.get("data", {}).get("quote")
    if not quote:
        raise DataSourceError("雪球未返回行情快照。")
    return quote


def normalize_xueqiu_values(
    company: CompanyIdentity,
    quote: dict[str, Any],
    captured_at: datetime,
) -> list[SourceValue]:
    url = build_xueqiu_url(company)
    definitions = [
        ("current_price", "current", lambda value: f"CNY {float(value):.2f}"),
        ("market_capitalization", "market_capital", lambda value: f"CNY {float(value) / 1_000_000_000:.2f}bn"),
        ("week_52_low", "low52w", lambda value: f"CNY {float(value):.2f}"),
        ("week_52_high", "high52w", lambda value: f"CNY {float(value):.2f}"),
    ]
    values: list[SourceValue] = []
    for field_id, key, formatter in definitions:
        raw = quote.get(key)
        if raw in (None, "-"):
            continue
        values.append(
            SourceValue(
                field_id=field_id,
                value=formatter(raw),
                raw_value=raw,
                source="xueqiu",
                source_url=url,
                captured_at=captured_at,
                metadata={"metric": key, "currency": "CNY"},
            )
        )
    return values

