from datetime import date
from decimal import Decimal

import httpx
import pytest

from mlc_agent.market_history import (
    MarketInstrument,
    MarketPoint,
    MarketSeries,
    align_market_series,
    analyze_market_performance,
    benchmark_for_company,
    fetch_market_series,
    market_history,
    maximum_drawdown,
)
from mlc_agent.schemas import CompanyIdentity


def _company(code, suffix="SZ", secid=None):
    market = "1" if suffix == "SH" else "0"
    return CompanyIdentity(
        company_name=f"Company {code}",
        company_short_name=f"Company {code}",
        stock_code=code,
        exchange={"SZ": "深圳证券交易所", "SH": "上海证券交易所", "BJ": "北京证券交易所"}[suffix],
        eastmoney_secid=secid or f"{market}.{code}",
        eastmoney_secu_code=f"{code}.{suffix}",
        xueqiu_symbol=f"{suffix}{code}",
    )


def _series(code, values, *, dates=None, kind="peer", adjustment="forward_adjusted"):
    dates = dates or [date(2024, 7, 3), date(2025, 7, 3), date(2026, 7, 3)]
    return MarketSeries(
        instrument=MarketInstrument(stock_code=code, display_name=code, eastmoney_secid=f"0.{code}", kind=kind),
        adjustment=adjustment,
        points=[
            MarketPoint(
                trading_date=day,
                close=Decimal(str(value)),
                high=Decimal(str(value + 1)) if adjustment == "unadjusted" else None,
                low=Decimal(str(value - 1)) if adjustment == "unadjusted" else None,
            )
            for day, value in zip(dates, values, strict=True)
        ],
        source_url=f"https://example.com/{code}/{adjustment}",
    )


def test_exchange_benchmark_mapping_is_fixed_for_all_three_exchanges():
    assert benchmark_for_company(_company("000938", "SZ")).eastmoney_secid == "0.399001"
    assert benchmark_for_company(_company("600519", "SH")).eastmoney_secid == "1.000001"
    assert benchmark_for_company(_company("430047", "BJ")).eastmoney_secid == "0.899050"


def test_alignment_uses_only_common_trading_dates_in_chronological_order():
    target = _series("target", [100, 110, 120], kind="target")
    benchmark = _series(
        "index",
        [90, 100, 110],
        dates=[date(2024, 7, 3), date(2025, 7, 4), date(2026, 7, 3)],
        kind="benchmark",
    )
    peers = [_series(str(index), [100, 100, 100]) for index in range(3)]
    aligned = align_market_series(target, benchmark, peers)
    assert aligned.start_date == date(2024, 7, 3)
    assert aligned.end_date == date(2026, 7, 3)
    assert [point.trading_date for point in aligned.target.points] == [
        date(2024, 7, 3),
        date(2026, 7, 3),
    ]


def test_15_percentage_point_alignment_boundary_is_inclusive():
    history = align_market_series(
        _series("target", [100, 115, 115], kind="target"),
        _series("index", [100, 100, 100], kind="benchmark"),
        [
            _series("p1", [100, 100, 100]),
            _series("p2", [100, 100, 100]),
            _series("p3", [100, 100, 100]),
        ],
    )
    result = analyze_market_performance(history)
    assert result.target_return == Decimal("0.15")
    assert result.aligns_to_index is True
    assert result.aligns_to_peers is True


def test_maximum_drawdown_keeps_peak_before_trough_and_30_percent_is_significant():
    series = _series(
        "target",
        [100, 120, 84, 130, 100],
        dates=[
            date(2024, 7, 3),
            date(2024, 8, 1),
            date(2024, 9, 1),
            date(2025, 1, 1),
            date(2025, 2, 1),
        ],
        kind="target",
    )
    drawdown = maximum_drawdown(series)
    assert drawdown.maximum_drawdown == Decimal("0.30")
    assert drawdown.peak_date == date(2024, 8, 1)
    assert drawdown.trough_date == date(2024, 9, 1)

    aligned = align_market_series(
        series,
        _series("index", [100, 100, 100, 100, 100], dates=[p.trading_date for p in series.points], kind="benchmark"),
        [_series(f"p{i}", [100, 100, 100, 100, 100], dates=[p.trading_date for p in series.points]) for i in range(3)],
    )
    assert analyze_market_performance(aligned).significant_drop is True


def test_market_history_fetches_one_adjusted_set_and_separate_unadjusted_snapshot():
    target = _company("000938")
    peers = [_company("600001", "SH"), _company("600002", "SH"), _company("600003", "SH")]
    calls = []

    def handler(request):
        calls.append((request.url.params["secid"], request.url.params["fqt"], request.url.params["beg"]))
        if request.url.params["fqt"] == "0":
            lines = [
                "2025-07-03,9,10,11,8,0,0,0,0,0,0",
                "2026-07-03,11,12,13,9,0,0,0,0,0,0",
            ]
        else:
            lines = [
                "2024-07-03,9,10,11,8,0,0,0,0,0,0",
                "2026-07-03,11,12,13,9,0,0,0,0,0,0",
            ]
        return httpx.Response(200, json={"data": {"klines": lines}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = market_history(
            client, target=target, peers=peers, as_of=date(2026, 7, 3),
            request_interval_seconds=0,
        )
    assert len(calls) == 6
    assert calls[:5] == [
        ("0.000938", "1", "20240703"),
        ("0.399001", "1", "20240703"),
        ("1.600001", "1", "20240703"),
        ("1.600002", "1", "20240703"),
        ("1.600003", "1", "20240703"),
    ]
    assert calls[5][0:2] == ("0.000938", "0")
    assert result.snapshot.current_price == Decimal("12")
    assert result.snapshot.week_52_high == Decimal("13")
    assert result.snapshot.week_52_low == Decimal("8")
    assert result.analysis.adjustment == "forward_adjusted"
    assert result.aligned_history.target.source_url


def test_market_history_rejects_missing_third_peer_without_backfill():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))) as client:
        with pytest.raises(ValueError, match="exactly three selected peers"):
            market_history(
                client,
                target=_company("000938"),
                peers=[_company("600001", "SH"), _company("600002", "SH")],
                as_of=date(2026, 7, 3),
            )


def test_market_history_rejects_incomplete_24_month_coverage():
    target = _company("000938")
    peers = [_company("600001", "SH"), _company("600002", "SH"), _company("600003", "SH")]

    def handler(request):
        if request.url.params["fqt"] == "0":
            lines = [
                "2025-07-03,9,10,11,8,0,0,0,0,0,0",
                "2026-07-03,11,12,13,9,0,0,0,0,0,0",
            ]
        else:
            lines = [
                "2025-07-03,9,10,11,8,0,0,0,0,0,0",
                "2026-07-03,11,12,13,9,0,0,0,0,0,0",
            ]
        return httpx.Response(200, json={"data": {"klines": lines}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="24-month start boundary"):
            market_history(
                client, target=target, peers=peers, as_of=date(2026, 7, 3),
                request_interval_seconds=0,
            )


def test_market_history_paces_all_six_eastmoney_requests():
    waits = []

    def handler(request):
        lines = [
            "2024-07-03,9,10,11,8,0,0,0,0,0,0",
            "2025-07-03,10,11,12,9,0,0,0,0,0,0",
            "2026-07-03,11,12,13,9,0,0,0,0,0,0",
        ]
        return httpx.Response(200, json={"data": {"klines": lines}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        market_history(
            client,
            target=_company("000938"),
            peers=[_company("000034"), _company("000977"), _company("603019", "SH")],
            as_of=date(2026, 7, 3),
            request_interval_seconds=1.0,
            sleeper=waits.append,
        )
    assert waits == [1.0] * 5


def test_fetch_market_series_retries_disconnect_with_cooldown_and_context():
    attempts = 0
    waits = []

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match=r"stock_code=000938.*adjustment=forward_adjusted"):
            fetch_market_series(
                client,
                instrument=MarketInstrument(
                    stock_code="000938", display_name="UNIS", eastmoney_secid="0.000938", kind="target"
                ),
                start_date=date(2024, 7, 3),
                end_date=date(2026, 7, 3),
                adjustment="forward_adjusted",
                retry_delays=(1.0, 2.0),
                sleeper=waits.append,
            )
    assert attempts == 3
    assert waits == [1.0, 2.0]
