import pytest
from calculate_metrics import (
    align_check,
    ar_growth_vs_revenue,
    capex_to_revenue,
    cash_flow_gt_noi,
    cumulative_return,
    current_ratio,
    debt_to_asset,
    growth_rate,
    high_low,
    intangibles_gt_25pct,
    interest_coverage,
    max_drawdown,
    ocf_positive,
    peer_median,
    quick_ratio,
    significant_drop,
    verify_chart,
)
from fixtures import metrics_input


def test_liquidity_formulas():
    assert current_ratio(1200, 600) == pytest.approx(2.0)
    assert quick_ratio(1200, 250, 600) == pytest.approx(950 / 600)
    assert capex_to_revenue(150, 9000) == pytest.approx(0.016666, rel=1e-3)
    assert interest_coverage(450, 40) == pytest.approx(12.25)
    assert debt_to_asset(1800, 3600) == pytest.approx(0.5)


def test_formulas_return_none_on_zero_denominator():
    assert current_ratio(1, 0) is None
    assert quick_ratio(1, 0, 0) is None
    assert capex_to_revenue(1, 0) is None
    assert interest_coverage(1, 0) is None
    assert debt_to_asset(1, 0) is None


def test_yes_no_flags():
    assert ocf_positive(380) is True
    assert ocf_positive(-1) is False
    assert cash_flow_gt_noi(380, 300) is True
    assert cash_flow_gt_noi(200, 300) is False
    assert ocf_positive(None) is None


def test_growth_and_ar_vs_revenue():
    assert growth_rate(8000, 9000) == pytest.approx(0.125)
    result = ar_growth_vs_revenue(600, 750, 8000, 9000)
    assert result["ar_growth"] == pytest.approx(0.25)
    assert result["revenue_growth"] == pytest.approx(0.125)
    assert result["ar_growth_higher"] is True
    assert ar_growth_vs_revenue(None, 750, 8000, 9000)["ar_growth_higher"] is None


def test_intangibles_threshold():
    assert intangibles_gt_25pct(100, 90, 3600) == {
        "intangibles_pct": pytest.approx(190 / 3600),
        "intangibles_gt_25pct": False,
    }
    assert intangibles_gt_25pct(500, 500, 1000)["intangibles_gt_25pct"] is True


def test_market_returns_and_drawdown():
    assert cumulative_return([100, 110, 120]) == pytest.approx(0.2)
    assert cumulative_return([100]) is None
    assert max_drawdown([100, 120, 60, 90]) == pytest.approx(0.5)
    assert max_drawdown([100, 101, 102]) == pytest.approx(0.0)
    assert high_low([5, 1, 9, 3]) == {"high": 9, "low": 1}


def test_align_and_significant_drop_boundaries():
    assert align_check(0.30, 0.15)["align"] is True
    assert align_check(0.300001, 0.15)["align"] is False
    assert align_check(None, 0.1)["align"] is None
    assert significant_drop(0.30) is True
    assert significant_drop(0.2999) is False


def test_peer_median():
    assert peer_median([1, 3, 5]) == 3
    assert peer_median([2, 4]) == 3
    assert peer_median([None, None]) is None


def test_compute_produces_charts_and_metrics(tmp_path):
    from calculate_metrics import compute

    result = compute(metrics_input(), chart_dir=tmp_path)
    assert result["liquidity"]["curr"]["current_ratio"] == pytest.approx(2.0)
    assert result["liquidity"]["short_term_debt_concern"] is True
    assert result["financial_analysis"]["ar_growth_higher"] is True
    assert result["peer_metrics"]["median"]["inventory_turnover"] == pytest.approx(3.95)
    for name in ("standalone-stock-chart.png", "competitor-comparison-chart.png"):
        dims = verify_chart(tmp_path / name)
        assert dims["width"] >= 1600
        assert dims["height"] >= 750
