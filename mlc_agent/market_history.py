from __future__ import annotations

import json

from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from time import sleep
from typing import Callable, Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from mlc_agent.schemas import CompanyIdentity


TENCENT_KLINE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_REFERER = "https://gu.qq.com/"
MARKET_REQUEST_INTERVAL_SECONDS = 1.0
MARKET_TRANSIENT_RETRY_DELAYS_SECONDS = (1.0, 2.0)
InstrumentKind = Literal["target", "peer", "benchmark"]
Adjustment = Literal["forward_adjusted", "unadjusted"]
ExchangeSuffix = Literal["SZ", "SH", "BJ"]


class MarketInstrument(BaseModel):
    stock_code: str
    display_name: str
    eastmoney_secid: str
    kind: InstrumentKind
    exchange_suffix: ExchangeSuffix | None = None


class MarketPoint(BaseModel):
    trading_date: date
    close: Decimal = Field(gt=0)
    high: Decimal | None = Field(default=None, gt=0)
    low: Decimal | None = Field(default=None, gt=0)


class MarketSeries(BaseModel):
    instrument: MarketInstrument
    adjustment: Adjustment
    points: list[MarketPoint] = Field(min_length=1)
    source_url: str

    @model_validator(mode="after")
    def validate_points(self) -> "MarketSeries":
        dates = [item.trading_date for item in self.points]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ValueError("market points must have unique ascending trading dates")
        if self.adjustment == "forward_adjusted" and any(
            item.high is not None or item.low is not None for item in self.points
        ):
            raise ValueError("forward-adjusted performance series contains close only")
        return self


class AlignedMarketHistory(BaseModel):
    start_date: date
    end_date: date
    target: MarketSeries
    benchmark: MarketSeries
    peers: list[MarketSeries] = Field(min_length=3, max_length=3)


class MarketSnapshot(BaseModel):
    quote_date: date
    current_price: Decimal
    week_52_high: Decimal
    week_52_low: Decimal
    source_url: str
    adjustment: Literal["unadjusted"] = "unadjusted"


class DrawdownResult(BaseModel):
    maximum_drawdown: Decimal
    peak_date: date
    trough_date: date
    peak_close: Decimal
    trough_close: Decimal


class MarketPerformanceAnalysis(BaseModel):
    target_return: Decimal
    benchmark_return: Decimal
    peer_returns: list[Decimal] = Field(min_length=3, max_length=3)
    peer_median_return: Decimal
    aligns_to_index: bool
    aligns_to_peers: bool
    maximum_drawdown: DrawdownResult
    significant_drop: bool
    start_date: date
    end_date: date
    adjustment: Literal["forward_adjusted"] = "forward_adjusted"


class MarketHistoryResult(BaseModel):
    aligned_history: AlignedMarketHistory
    snapshot: MarketSnapshot
    analysis: MarketPerformanceAnalysis


_BENCHMARKS = {
    "SZ": MarketInstrument(
        stock_code="399001",
        display_name="Shenzhen Component Index",
        eastmoney_secid="0.399001",
        kind="benchmark",
        exchange_suffix="SZ",
    ),
    "SH": MarketInstrument(
        stock_code="000001",
        display_name="SSE Composite Index",
        eastmoney_secid="1.000001",
        kind="benchmark",
        exchange_suffix="SH",
    ),
    "BJ": MarketInstrument(
        stock_code="899050",
        display_name="Beijing Stock Exchange 50 Index",
        eastmoney_secid="0.899050",
        kind="benchmark",
        exchange_suffix="BJ",
    ),
}


def benchmark_for_company(company: CompanyIdentity) -> MarketInstrument:
    suffix = company.eastmoney_secu_code.rsplit(".", 1)[-1].upper()
    try:
        return _BENCHMARKS[suffix].model_copy(deep=True)
    except KeyError as exc:
        raise ValueError(f"unsupported exchange suffix for benchmark: {suffix}") from exc


def subtract_calendar_months(value: date, months: int) -> date:
    if months < 0:
        raise ValueError("months must be non-negative")
    absolute_month = value.year * 12 + value.month - 1 - months
    year, zero_month = divmod(absolute_month, 12)
    month = zero_month + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _instrument(company: CompanyIdentity, kind: Literal["target", "peer"]) -> MarketInstrument:
    suffix = company.eastmoney_secu_code.rsplit(".", 1)[-1].upper()
    if suffix not in {"SZ", "SH", "BJ"}:
        raise ValueError(f"unsupported exchange suffix for market history: {suffix}")
    return MarketInstrument(
        stock_code=company.stock_code,
        display_name=company.company_short_name,
        eastmoney_secid=company.eastmoney_secid,
        kind=kind,
        exchange_suffix=suffix,
    )


def _parse_kline(
    lines: list[str],
    *,
    instrument: MarketInstrument,
    adjustment: Adjustment,
    source_url: str,
) -> MarketSeries:
    points: list[MarketPoint] = []
    for line in lines:
        columns = str(line).split(",")
        if len(columns) < 5:
            raise ValueError(f"invalid K-line row for {instrument.stock_code}")
        point = MarketPoint(
            trading_date=date.fromisoformat(columns[0]),
            close=Decimal(columns[2]),
            high=Decimal(columns[3]) if adjustment == "unadjusted" else None,
            low=Decimal(columns[4]) if adjustment == "unadjusted" else None,
        )
        points.append(point)
    return MarketSeries(
        instrument=instrument,
        adjustment=adjustment,
        points=points,
        source_url=source_url,
    )


def fetch_market_series(
    client: httpx.Client,
    *,
    instrument: MarketInstrument,
    start_date: date,
    end_date: date,
    adjustment: Adjustment,
    retry_delays: tuple[float, ...] = MARKET_TRANSIENT_RETRY_DELAYS_SECONDS,
    sleeper: Callable[[float], None] = sleep,
) -> MarketSeries:
    suffix = instrument.exchange_suffix
    if suffix is None:
        raise ValueError(
            f"exchange_suffix is required for market history: {instrument.stock_code}"
        )
    exchange_prefix = {"SZ": "sz", "SH": "sh", "BJ": "bj"}[suffix]
    symbol = f"{exchange_prefix}{instrument.stock_code}"
    adjusted = adjustment == "forward_adjusted"
    params = {
        "_var": "kline_dayqfq" if adjusted else "kline_day",
        "param": (
            f"{symbol},day,{start_date.isoformat()},{end_date.isoformat()},1000,"
            f"{'qfq' if adjusted else ''}"
        ),
    }
    transient_errors = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
    )
    for attempt in range(len(retry_delays) + 1):
        try:
            response = client.get(
                TENCENT_KLINE_API,
                params=params,
                headers={"Referer": TENCENT_REFERER, "User-Agent": "Mozilla/5.0"},
            )
            break
        except transient_errors as exc:
            if attempt == len(retry_delays):
                raise RuntimeError(
                    "Tencent market history request failed after "
                    f"{attempt + 1} attempts: stock_code={instrument.stock_code}, "
                    f"symbol={symbol}, adjustment={adjustment}, "
                    f"range={start_date.isoformat()}..{end_date.isoformat()}"
                ) from exc
            sleeper(retry_delays[attempt])
    response.raise_for_status()
    raw = response.text.strip()
    if "=" in raw:
        raw = raw.split("=", 1)[1].strip().rstrip(";")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"invalid Tencent market history response for {instrument.stock_code}") from exc
    if not isinstance(payload, dict) or payload.get("code") not in {0, "0"}:
        raise ValueError(f"Tencent market history returned an invalid result for {instrument.stock_code}")
    instrument_data = ((payload.get("data") or {}).get(symbol) or {})
    if adjusted and instrument.kind == "benchmark":
        # Broad-market indices have no corporate-action adjustment.  Tencent
        # therefore exposes their valid daily series under ``day`` even when
        # the request uses the qfq endpoint variable.
        rows = instrument_data.get("qfqday") or instrument_data.get("day") or []
    else:
        rows = instrument_data.get("qfqday" if adjusted else "day") or []
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"no market history for {instrument.stock_code}")
    lines = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            raise ValueError(f"invalid Tencent K-line row for {instrument.stock_code}")
        lines.append(
            ",".join(
                [
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    str(row[4]),
                ]
            )
        )
    return _parse_kline(
        lines,
        instrument=instrument,
        adjustment=adjustment,
        source_url=str(response.url),
    )


def align_market_series(
    target: MarketSeries,
    benchmark: MarketSeries,
    peers: list[MarketSeries],
) -> AlignedMarketHistory:
    if len(peers) != 3:
        raise ValueError("exactly three peer market series are required")
    all_series = [target, benchmark, *peers]
    if any(item.adjustment != "forward_adjusted" for item in all_series):
        raise ValueError("performance alignment requires forward-adjusted series")
    common_dates = set(point.trading_date for point in target.points)
    for series in all_series[1:]:
        common_dates &= {point.trading_date for point in series.points}
    ordered_dates = sorted(common_dates)
    if len(ordered_dates) < 2:
        raise ValueError("fewer than two common trading dates")

    def aligned(series: MarketSeries) -> MarketSeries:
        selected = [point for point in series.points if point.trading_date in common_dates]
        return series.model_copy(update={"points": selected})

    return AlignedMarketHistory(
        start_date=ordered_dates[0],
        end_date=ordered_dates[-1],
        target=aligned(target),
        benchmark=aligned(benchmark),
        peers=[aligned(item) for item in peers],
    )


def cumulative_return(series: MarketSeries) -> Decimal:
    if len(series.points) < 2:
        raise ValueError("cumulative return requires at least two points")
    first = series.points[0].close
    return series.points[-1].close / first - Decimal("1")


def maximum_drawdown(series: MarketSeries) -> DrawdownResult:
    if len(series.points) < 2:
        raise ValueError("maximum drawdown requires at least two points")
    peak = series.points[0]
    deepest: tuple[Decimal, MarketPoint, MarketPoint] | None = None
    for point in series.points[1:]:
        if point.close > peak.close:
            peak = point
            continue
        drawdown = (peak.close - point.close) / peak.close
        if deepest is None or drawdown > deepest[0]:
            deepest = (drawdown, peak, point)
    if deepest is None:
        last = series.points[-1]
        deepest = (Decimal("0"), last, last)
    value, peak_point, trough_point = deepest
    return DrawdownResult(
        maximum_drawdown=value,
        peak_date=peak_point.trading_date,
        trough_date=trough_point.trading_date,
        peak_close=peak_point.close,
        trough_close=trough_point.close,
    )


def analyze_market_performance(history: AlignedMarketHistory) -> MarketPerformanceAnalysis:
    target_return = cumulative_return(history.target)
    benchmark_return = cumulative_return(history.benchmark)
    peer_returns = [cumulative_return(item) for item in history.peers]
    peer_median = Decimal(str(median(peer_returns)))
    drawdown = maximum_drawdown(history.target)
    return MarketPerformanceAnalysis(
        target_return=target_return,
        benchmark_return=benchmark_return,
        peer_returns=peer_returns,
        peer_median_return=peer_median,
        aligns_to_index=abs(target_return - benchmark_return) <= Decimal("0.15"),
        aligns_to_peers=abs(target_return - peer_median) <= Decimal("0.15"),
        maximum_drawdown=drawdown,
        significant_drop=drawdown.maximum_drawdown >= Decimal("0.30"),
        start_date=history.start_date,
        end_date=history.end_date,
    )


def build_unadjusted_snapshot(series: MarketSeries, *, as_of: date) -> MarketSnapshot:
    if series.adjustment != "unadjusted":
        raise ValueError("snapshot requires unadjusted market history")
    eligible = [item for item in series.points if item.trading_date <= as_of]
    if not eligible:
        raise ValueError("no unadjusted market point on or before as_of")
    latest = eligible[-1]
    trailing_start = latest.trading_date - timedelta(days=365)
    if eligible[0].trading_date > trailing_start:
        raise ValueError("unadjusted history does not cover the full trailing 365-day window")
    trailing = [item for item in eligible if item.trading_date >= trailing_start]
    if any(item.high is None or item.low is None for item in trailing):
        raise ValueError("unadjusted history is missing daily high or low")
    return MarketSnapshot(
        quote_date=latest.trading_date,
        current_price=latest.close,
        week_52_high=max(item.high for item in trailing if item.high is not None),
        week_52_low=min(item.low for item in trailing if item.low is not None),
        source_url=series.source_url,
    )


def market_history(
    client: httpx.Client,
    *,
    target: CompanyIdentity,
    peers: list[CompanyIdentity],
    as_of: date,
    request_interval_seconds: float = MARKET_REQUEST_INTERVAL_SECONDS,
    sleeper: Callable[[float], None] = sleep,
) -> MarketHistoryResult:
    """The single shared market-history interface for parts 8 and 9."""
    if len(peers) != 3:
        raise ValueError("market_history requires exactly three selected peers")
    if request_interval_seconds < 0:
        raise ValueError("request_interval_seconds must be non-negative")
    start = subtract_calendar_months(as_of, 24)
    requests = [
        (_instrument(target, "target"), start, "forward_adjusted"),
        (benchmark_for_company(target), start, "forward_adjusted"),
        *((_instrument(peer, "peer"), start, "forward_adjusted") for peer in peers),
        (_instrument(target, "target"), as_of - timedelta(days=370), "unadjusted"),
    ]
    series: list[MarketSeries] = []
    for index, (instrument, begin, adjustment) in enumerate(requests):
        if index and request_interval_seconds:
            sleeper(request_interval_seconds)
        series.append(fetch_market_series(
            client,
            instrument=instrument,
            start_date=begin,
            end_date=as_of,
            adjustment=adjustment,
            sleeper=sleeper,
        ))
    target_adjusted, benchmark_adjusted, *remainder = series
    peer_adjusted = remainder[:3]
    unadjusted = remainder[3]
    aligned = align_market_series(target_adjusted, benchmark_adjusted, peer_adjusted)
    if aligned.start_date > start + timedelta(days=7):
        raise ValueError("aligned adjusted history does not cover the 24-month start boundary")
    if aligned.end_date < as_of - timedelta(days=7):
        raise ValueError("aligned adjusted history does not reach the as_of boundary")
    return MarketHistoryResult(
        aligned_history=aligned,
        snapshot=build_unadjusted_snapshot(unadjusted, as_of=as_of),
        analysis=analyze_market_performance(aligned),
    )
