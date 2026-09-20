#!/usr/bin/env python3
"""Deterministic financial and market calculations plus the two stock charts.

This script never calls a model and never chooses peers. It only turns raw,
already-cited inputs into reproducible numbers and chart images. See
``references/evidence-policy.md`` for the fixed formulas.

Input JSON shape (all financial values are plain numbers in one currency)::

    {
      "target": {"name": "...", "ticker": "..."},
      "peers": [{"name": "...", "ticker": "..."}, {"name": "...", "ticker": "..."}],
      "periods": {"prev": "2023A", "curr": "2024A"},
      "financials": {
        "prev": {"current_assets": 0, "current_liabilities": 0, "inventory": 0,
                  "capex": 0, "revenue": 0, "operating_profit": 0,
                  "interest_expense": 0, "total_liabilities": 0, "total_assets": 0,
                  "ocf": 0, "net_income": 0, "accounts_receivable": 0,
                  "goodwill": 0, "intangibles": 0, "cash": 0,
                  "short_term_borrowings": 0},
        "curr": { ... same keys ... }
      },
      "market": {
        "target": {"dates": ["2023-01-03", ...], "adj_close": [...], "close": [...]},
        "peers": [{"name": "...", "dates": [...], "adj_close": [...]}, ...],
        "index": {"name": "...", "dates": [...], "close": [...]}
      },
      "peer_metrics": {
        "target": {"revenue": 0, "inventory_turnover": 0, "gross_margin": 0, "net_margin": 0},
        "peers": [{"name": "...", "revenue": 0, "inventory_turnover": 0,
                    "gross_margin": 0, "net_margin": 0}, ...]
      }
    }
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

# Render CJK company names when no official English name exists.
matplotlib.rcParams["font.sans-serif"] = [
    "Arial Unicode MS", "PingFang SC", "Heiti SC", "STHeiti",
    "Hiragino Sans GB", "Songti SC", "DejaVu Sans",
]
matplotlib.rcParams["axes.unicode_minus"] = False

ALIGN_TOLERANCE = 0.15
SIGNIFICANT_DRAWDOWN = 0.30
CHART_WIDTH_INCHES = 6.3
CHART_DPI = 300
MIN_CHART_PX = (1600, 750)


# --------------------------------------------------------------------------- #
# Pure calculations
# --------------------------------------------------------------------------- #
def _div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def current_ratio(current_assets, current_liabilities):
    return _div(current_assets, current_liabilities)


def quick_ratio(current_assets, inventory, current_liabilities):
    if current_assets is None or inventory is None:
        return None
    return _div(current_assets - inventory, current_liabilities)


def capex_to_revenue(capex, revenue):
    return _div(capex, revenue)


def interest_coverage(operating_profit, interest_expense):
    if operating_profit is None or interest_expense in (None, 0):
        return None
    return (operating_profit + interest_expense) / interest_expense


def debt_to_asset(total_liabilities, total_assets):
    return _div(total_liabilities, total_assets)


def ocf_positive(ocf) -> bool | None:
    return None if ocf is None else ocf > 0


def cash_flow_gt_noi(ocf, noi) -> bool | None:
    if ocf is None or noi is None:
        return None
    return ocf > noi


def growth_rate(previous, current) -> float | None:
    if previous in (None, 0) or current is None:
        return None
    return (current - previous) / abs(previous)


def ar_growth_vs_revenue(
    ar_begin, ar_end, revenue_prev, revenue_curr
) -> dict[str, Any]:
    ar_growth = growth_rate(ar_begin, ar_end)
    revenue_growth = growth_rate(revenue_prev, revenue_curr)
    higher = None
    if ar_growth is not None and revenue_growth is not None:
        higher = ar_growth > revenue_growth
    return {
        "ar_growth": ar_growth,
        "revenue_growth": revenue_growth,
        "ar_growth_higher": higher,
    }


def intangibles_gt_25pct(goodwill, intangibles, total_assets) -> dict[str, Any]:
    if goodwill is None or intangibles is None or not total_assets:
        return {"intangibles_pct": None, "intangibles_gt_25pct": None}
    pct = (goodwill + intangibles) / total_assets
    return {"intangibles_pct": pct, "intangibles_gt_25pct": pct > 0.25}


def cumulative_return(prices: Sequence[float]) -> float | None:
    clean = [p for p in prices if p is not None]
    if len(clean) < 2 or clean[0] == 0:
        return None
    return clean[-1] / clean[0] - 1.0


def max_drawdown(prices: Sequence[float]) -> float | None:
    """Maximum peak-to-trough decline as a positive fraction (0.35 == -35%)."""
    peak = None
    worst = 0.0
    seen = False
    for p in prices:
        if p is None:
            continue
        seen = True
        peak = p if peak is None or p > peak else peak
        if peak:
            worst = max(worst, (peak - p) / peak)
    return worst if seen else None


def high_low(closes: Sequence[float]) -> dict[str, float | None]:
    clean = [p for p in closes if p is not None]
    if not clean:
        return {"high": None, "low": None}
    return {"high": max(clean), "low": min(clean)}


def align_check(target_return, benchmark_return, tolerance=ALIGN_TOLERANCE):
    if target_return is None or benchmark_return is None:
        return {"align": None, "difference_pp": None}
    diff = target_return - benchmark_return
    return {"align": abs(diff) <= tolerance, "difference_pp": diff * 100.0}


def peer_median(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return statistics.median(clean)


def significant_drop(drawdown, threshold=SIGNIFICANT_DRAWDOWN):
    if drawdown is None:
        return None
    return drawdown >= threshold


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def _parse_dates(dates: Sequence[str]) -> list[date]:
    return [date.fromisoformat(d) for d in dates]


def _new_figure():
    fig, ax = plt.subplots(figsize=(CHART_WIDTH_INCHES, CHART_WIDTH_INCHES / 2))
    return fig, ax


def _finish(fig, out_path: Path) -> Path:
    fig.tight_layout()
    fig.savefig(out_path, dpi=CHART_DPI)
    plt.close(fig)
    return out_path


def render_standalone_chart(
    target: dict[str, Any], out_path: str | Path
) -> Path:
    fig, ax = _new_figure()
    dates = mdates.date2num(_parse_dates(target["dates"]))
    ax.plot(dates, target["adj_close"], color="#c0392b", linewidth=1.3)
    ax.set_title(f"{target['name']} forward-adjusted close (2 years)")
    ax.set_ylabel("Adjusted price")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    return _finish(fig, Path(out_path))


def render_comparison_chart(
    target: dict[str, Any], peers: Sequence[dict[str, Any]], out_path: str | Path
) -> Path:
    fig, ax = _new_figure()
    for series, color in zip(
        [target, *peers], ["#c0392b", "#2980b9", "#27ae60"], strict=False
    ):
        prices = series["adj_close"]
        if not prices or prices[0] == 0:
            continue
        normalised = [p / prices[0] * 100.0 for p in prices]
        ax.plot(mdates.date2num(_parse_dates(series["dates"])), normalised,
                label=series["name"], color=color, linewidth=1.3)
    ax.axhline(100, color="#7f8c8d", linewidth=0.8, linestyle="--")
    ax.set_title("Normalised to 100 on first day (2 years)")
    ax.set_ylabel("Index (first day = 100)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    return _finish(fig, Path(out_path))


def verify_chart(path: str | Path) -> dict[str, int]:
    """Read a PNG's pixel dimensions from the IHDR chunk (no image dependency)."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return {"width": width, "height": height}


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def _period_block(metrics: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "current_ratio": current_ratio(
            raw.get("current_assets"), raw.get("current_liabilities")
        ),
        "quick_ratio": quick_ratio(
            raw.get("current_assets"), raw.get("inventory"),
            raw.get("current_liabilities"),
        ),
        "capex_to_revenue": capex_to_revenue(raw.get("capex"), raw.get("revenue")),
        "interest_coverage": interest_coverage(
            raw.get("operating_profit"), raw.get("interest_expense")
        ),
        "debt_to_asset": debt_to_asset(
            raw.get("total_liabilities"), raw.get("total_assets")
        ),
        "ocf_positive": ocf_positive(raw.get("ocf")),
        "cash_flow_gt_noi": cash_flow_gt_noi(raw.get("ocf"), raw.get("net_income")),
        "capex": raw.get("capex"),
        "revenue": raw.get("revenue"),
        "cash": raw.get("cash"),
        "short_term_borrowings": raw.get("short_term_borrowings"),
        "net_income": raw.get("net_income"),
    }


def compute(spec: dict[str, Any], chart_dir: str | Path | None = None) -> dict[str, Any]:
    periods = spec.get("periods", {})
    financials = spec.get("financials", {})
    prev = _period_block({}, financials.get("prev", {}))
    curr = _period_block({}, financials.get("curr", {}))
    liquidity = {
        "periods": periods,
        "prev": prev,
        "curr": curr,
        "short_term_debt_concern": (
            None
            if curr["cash"] is None or curr["short_term_borrowings"] is None
            else curr["cash"] < curr["short_term_borrowings"]
        ),
    }

    fa = ar_growth_vs_revenue(
        financials.get("prev", {}).get("accounts_receivable"),
        financials.get("curr", {}).get("accounts_receivable"),
        financials.get("prev", {}).get("revenue"),
        financials.get("curr", {}).get("revenue"),
    )
    fa.update(
        intangibles_gt_25pct(
            financials.get("curr", {}).get("goodwill"),
            financials.get("curr", {}).get("intangibles"),
            financials.get("curr", {}).get("total_assets"),
        )
    )

    market = spec.get("market", {})
    target_market = market.get("target", {})
    target_ret = cumulative_return(target_market.get("adj_close", []))
    target_dd = max_drawdown(target_market.get("adj_close", []))
    hl = high_low(target_market.get("close", []))

    index = market.get("index", {})
    index_ret = cumulative_return(index.get("close", []))
    align_index = align_check(target_ret, index_ret)

    peer_returns = [cumulative_return(p.get("adj_close", [])) for p in market.get("peers", [])]
    peer_ret_median = peer_median(peer_returns)
    align_peers = align_check(target_ret, peer_ret_median)

    result: dict[str, Any] = {
        "target": spec.get("target", {}),
        "peers": spec.get("peers", []),
        "periods": periods,
        "liquidity": liquidity,
        "financial_analysis": fa,
        "market": {
            "target": {
                "cumulative_return_24m": target_ret,
                "max_drawdown_24m": target_dd,
                "significant_drop": significant_drop(target_dd),
                "current_price": (target_market.get("close") or [None])[-1],
                "high_52w": hl["high"],
                "low_52w": hl["low"],
            },
            "index": {"name": index.get("name"), "cumulative_return_24m": index_ret},
            "align_index": align_index,
            "peer_returns": peer_returns,
            "peer_median_return": peer_ret_median,
            "align_peers": align_peers,
        },
    }

    pm = spec.get("peer_metrics", {})
    peer_metrics = pm.get("peers", [])
    result["peer_metrics"] = {
        "target": pm.get("target", {}),
        "peers": peer_metrics,
        "median": {
            key: peer_median([p.get(key) for p in peer_metrics])
            for key in ("revenue", "inventory_turnover", "gross_margin", "net_margin")
        },
    }

    if chart_dir is not None:
        chart_dir = Path(chart_dir)
        chart_dir.mkdir(parents=True, exist_ok=True)
        standalone = chart_dir / "standalone-stock-chart.png"
        comparison = chart_dir / "competitor-comparison-chart.png"
        target_series = dict(target_market)
        target_series.setdefault("name", spec.get("target", {}).get("name", "Target"))
        if target_market.get("dates"):
            render_standalone_chart(target_series, standalone)
        if target_market.get("dates") and market.get("peers"):
            render_comparison_chart(target_series, market["peers"], comparison)
        result["charts"] = {
            "standalone": str(standalone) if standalone.exists() else None,
            "comparison": str(comparison) if comparison.exists() else None,
        }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="metrics input JSON")
    parser.add_argument("--output", required=True, help="metrics output JSON")
    parser.add_argument("--chart-dir", help="directory for the two chart PNGs")
    args = parser.parse_args(argv)

    with open(args.input, encoding="utf-8") as fh:
        spec = json.load(fh)
    result = compute(spec, chart_dir=args.chart_dir)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out), "charts": result.get("charts", {})},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
