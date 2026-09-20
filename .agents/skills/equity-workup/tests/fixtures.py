"""Shared fixtures for the equity-workup tests and the acceptance run.

Builds a complete, schema-valid run directory using the real Word template and
clearly-labelled synthetic evidence. The synthetic values exist only to exercise
the deterministic tooling; they are not real research and must never be
presented as such.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = REPO_ROOT / "Workup_template_260617-外测版.docx"
ORIGINAL_SHA256 = "87431ae9373b9fd7251f8eeb0aea23a509f49368642d764e6426862168f4d537"

CAPTURED_AT = "2026-09-20T10:00:00+08:00"
TARGET = {"name": "浪潮信息", "ticker": "000977", "exchange": "SZSE"}
PEERS = [
    {"name": "神州数码", "ticker": "000034", "exchange": "SZSE"},
    {"name": "中科曙光", "ticker": "603019", "exchange": "SSE"},
]


def run_plan() -> dict:
    return {
        "target": TARGET,
        "peers": PEERS,
        "as_of_date": "2026-09-20",
        "peer_selection": {"mode": "user_named"},
        "fields_source": "references/report-contract.md",
        "retry_plan": {"max_paths_per_field": 2, "stop_after_no_progress_rounds": 1},
    }


def _citation(url: str = "https://example.com/annual-2024.pdf", title: str = "2024 Annual Report",
              source_type: str = "annual_report", quote: str = "…", period: str = "2024A",
              section: str = "Section 3") -> dict:
    return {
        "period": period,
        "source_url": url,
        "source_title": title,
        "source_type": source_type,
        "published_at": "2025-03-28",
        "captured_at": CAPTURED_AT,
        "page_or_section": section,
        "evidence_quote": quote,
    }


def supported(value, **kw) -> dict:
    record = {"status": "supported", "value": value, "calculation": None, "derived_from": []}
    record.update(_citation(**kw))
    return record


def derived(value, calculation: str, derived_from: list[str]) -> dict:
    return {
        "status": "derived",
        "value": value,
        "calculation": calculation,
        "derived_from": derived_from,
    }


def not_disclosed(value="Not disclosed", **kw) -> dict:
    record = {"status": "not_disclosed", "value": value, "calculation": None, "derived_from": []}
    record.update(_citation(**kw))
    return record


def not_applicable(value="Not applicable", **kw) -> dict:
    record = {"status": "not_applicable", "value": value, "calculation": None, "derived_from": []}
    record.update(_citation(**kw))
    return record


def unresolved(reason: str, attempts: list[str]) -> dict:
    return {"status": "unresolved", "reason": reason, "attempts": attempts}


def metrics_input() -> dict:
    days = []
    day = dt.date(2023, 1, 2)
    while day <= dt.date(2024, 12, 31):
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += dt.timedelta(days=1)

    def series(base, drift):
        return [round(base * (1 + drift * i / len(days)), 2) for i in range(len(days))]

    return {
        "target": TARGET,
        "peers": PEERS,
        "periods": {"prev": "2023A", "curr": "2024A"},
        "financials": {
            "prev": {
                "current_assets": 1000, "current_liabilities": 500, "inventory": 200,
                "capex": 120, "revenue": 8000, "operating_profit": 400,
                "interest_expense": 50, "total_liabilities": 1500, "total_assets": 3000,
                "ocf": 300, "net_income": 250, "accounts_receivable": 600,
                "goodwill": 100, "intangibles": 80, "cash": 300,
                "short_term_borrowings": 200,
            },
            "curr": {
                "current_assets": 1200, "current_liabilities": 600, "inventory": 250,
                "capex": 150, "revenue": 9000, "operating_profit": 450,
                "interest_expense": 40, "total_liabilities": 1800, "total_assets": 3600,
                "ocf": 380, "net_income": 300, "accounts_receivable": 750,
                "goodwill": 100, "intangibles": 90, "cash": 280,
                "short_term_borrowings": 320,
            },
        },
        "market": {
            "target": {"name": TARGET["name"], "dates": days,
                       "adj_close": series(30, 0.10), "close": series(30, 0.10)},
            "peers": [
                {"name": PEERS[0]["name"], "dates": days, "adj_close": series(20, 0.05)},
                {"name": PEERS[1]["name"], "dates": days, "adj_close": series(40, -0.08)},
            ],
            "index": {"name": "深证成指", "dates": days, "close": series(11000, 0.03)},
        },
        "peer_metrics": {
            "target": {"revenue": 9000, "inventory_turnover": 5.2, "gross_margin": 0.12,
                       "net_margin": 0.033},
            "peers": [
                {"name": PEERS[0]["name"], "revenue": 11000, "inventory_turnover": 4.8,
                 "gross_margin": 0.10, "net_margin": 0.02},
                {"name": PEERS[1]["name"], "revenue": 7000, "inventory_turnover": 3.1,
                 "gross_margin": 0.24, "net_margin": 0.09},
            ],
        },
    }


def evidence(metrics: dict) -> dict:
    m = metrics
    liq = m["liquidity"]
    fa = m["financial_analysis"]
    market = m["market"]

    fields: dict[str, dict] = {
        # Company overview
        "co.country_of_incorp": supported("PRC", quote="注册地：中国"),
        "co.soe_status": supported("Non-SOE", quote="实际控制人为自然人"),
        "co.total_asset": supported("RMB 360,000,000,000", quote="总资产 360,000,000,000 元"),
        "co.total_equity": supported(
            "RMB 180,000,000,000", quote="所有者权益合计 180,000,000,000 元"),
        "co.annual_revenue": supported("RMB 90,000,000,000", quote="营业收入 90,000,000,000 元"),
        "co.annual_net_profit": supported("RMB 3,000,000,000", quote="归母净利润 3,000,000,000 元"),
        "co.listed_or_private": supported("Listed", quote="本公司为深交所上市公司"),
        "co.listed_exchange": supported("Shenzhen Stock Exchange", quote="深圳证券交易所"),
        "co.market_cap": supported("RMB 120,000,000,000", source_type="market_data",
                                   url="https://example.com/quote", title="Market quote",
                                   quote="总市值 120,000,000,000 元"),
        "co.listed_subsidiary": supported("No", quote="无上市子公司"),
        "co.listed_outside_directorship": supported("No", quote="董事无其他上市公司任职"),
        "co.ma_past_12m": supported("None", quote="报告期内无重大资产重组"),
        "co.ma_plan_next_12m": not_disclosed(quote="未披露未来十二个月并购计划"),
        "co.website": supported("https://www.inspur.com", source_type="company_official",
                                url="https://www.inspur.com", title="Official website",
                                quote="官网"),
        "co.google_finance": supported("https://www.google.com/finance/quote/000977:SHE",
                                       source_type="market_data",
                                       url="https://www.google.com/finance/quote/000977:SHE",
                                       title="Google Finance", quote="000977"),
        "co.cninfo": supported("http://www.cninfo.com.cn/", source_type="exchange_filing",
                               url="http://www.cninfo.com.cn/", title="巨潮资讯网",
                               quote="巨潮资讯网"),
        "co.xueqiu": supported("https://xueqiu.com/S/SZ000977", source_type="market_data",
                               url="https://xueqiu.com/S/SZ000977", title="雪球", quote="雪球"),
        "co.eastmoney": supported("https://quote.eastmoney.com/sz000977.html",
                                  source_type="market_data",
                                  url="https://quote.eastmoney.com/sz000977.html",
                                  title="东方财富", quote="东方财富"),
        "co.business_description": supported(
            "Inspur Information is a server and cloud computing supplier.\n"
            "Founded in 1998 and listed on the SZSE in 2000.",
            quote="浪潮信息主营服务器及云计算"),
        # US exposure
        "us.subsidiaries": supported("Inspur USA Inc.", quote="美国子公司 Inspur USA Inc."),
        "us.num_subsidiaries": supported("1", quote="1 家美国子公司"),
        "us.revenue": unresolved("US revenue not separately disclosed",
                                 ["annual report segment note", "10-K equivalent not available"]),
        "us.split_by_state": not_disclosed(quote="年报未按州披露员工分布"),
        # Operating performance
        "op.revenue_breakdown": supported(
            "Servers 82.0%; IT components 12.0%; Other 6.0%", quote="服务器收入占比 82.0%"),
        "op.revenue_trend": supported(
            "2023Q1 12.0bn; 2023Q2 13.5bn; 2023Q3 14.1bn; 2023Q4 15.4bn",
            quote="分季度营业收入"),
        "op.outlook_risks": supported("Customer concentration remains the main risk.",
                                      quote="前五大客户收入占比较高"),
        "op.related_party": supported("Routine related-party purchases on standard terms.",
                                      quote="关联采购按市场公允价格进行"),
        "op.peer_confirmation": derived(
            "Revenue trend broadly in line with peers; target net margin below peer median.",
            "comparison of target and peer median net margin and revenue growth",
            ["op.peer.proposer.net_margin", "op.peer1.net_margin", "op.peer2.net_margin"]),
        "op.peer.proposer.name": supported(TARGET["name"], quote="浪潮信息"),
        "op.peer.proposer.revenue": derived("RMB 90,000,000,000",
                                            "revenue 2024A", ["co.annual_revenue"]),
        "op.peer.proposer.inventory_turnover": derived(
            "5.20", "provider disclosed inventory turnover", ["co.annual_revenue"]),
        "op.peer.proposer.gross_margin": derived(
            "12.00%", "gross profit / revenue", ["co.annual_revenue"]),
        "op.peer.proposer.net_margin": derived(
            "3.33%", "net income attributable / revenue",
            ["co.annual_net_profit", "co.annual_revenue"]),
        "op.peer1.name": supported(PEERS[0]["name"], quote="神州数码"),
        "op.peer1.revenue": derived("RMB 110,000,000,000", "peer revenue 2024A",
                                    ["op.peer1.name"]),
        "op.peer1.inventory_turnover": derived("4.80", "provider disclosed",
                                               ["op.peer1.name"]),
        "op.peer1.gross_margin": derived("10.00%", "gross profit / revenue", ["op.peer1.name"]),
        "op.peer1.net_margin": derived("2.00%", "net income attributable / revenue",
                                       ["op.peer1.name"]),
        "op.peer2.name": supported(PEERS[1]["name"], quote="中科曙光"),
        "op.peer2.revenue": derived("RMB 70,000,000,000", "peer revenue 2024A",
                                    ["op.peer2.name"]),
        "op.peer2.inventory_turnover": derived("3.10", "provider disclosed",
                                               ["op.peer2.name"]),
        "op.peer2.gross_margin": derived("24.00%", "gross profit / revenue", ["op.peer2.name"]),
        "op.peer2.net_margin": derived("9.00%", "net income attributable / revenue",
                                       ["op.peer2.name"]),
        # Liquidity
        "liq.comments": derived("Adequate liquidity; short-term borrowings covered by cash.",
                                "synthesis of liquidity ratios", ["liq.current_ratio_curr"]),
        "liq.short_term_debt_concern": derived("Yes", "cash < short-term borrowings",
                                               ["liq.current_ratio_curr"]),
        "liq.positive_ocf": derived("Yes", "both years OCF > 0", ["liq.ocf_positive_curr"]),
        "liq.net_income_exceed_ocf": derived(
            "No", "OCF > net income", ["liq.cashflow_gt_noi_curr"]),
        # Financial analysis
        "fin.ar_growth_vs_revenue": derived(
            "Yes",
            f"AR growth {fa['ar_growth']:.2%} > revenue growth {fa['revenue_growth']:.2%}",
            ["co.annual_revenue"]),
        "fin.one_time_writedown": derived("No", "no material one-time write-down",
                                          ["co.annual_net_profit"]),
        "fin.intangibles_gt_25pct": derived("No",
                                            f"intangibles {fa['intangibles_pct']:.2%} < 25%",
                                            ["co.total_asset"]),
        # Security exposure
        "sec.ipo_date": supported("2000-06-08", quote="2000年6月8日上市"),
        "sec.total_market_cap": supported("RMB 120,000,000,000", source_type="market_data",
                                          url="https://example.com/quote", title="Quote",
                                          quote="总市值"),
        "sec.current_price": supported("RMB 32.99", source_type="market_data",
                                       url="https://example.com/quote", title="Quote",
                                       quote="最新价 32.99"),
        "sec.52w_low": supported("RMB 30.00", source_type="market_data",
                                 url="https://example.com/quote", title="Quote",
                                 quote="52周最低 30.00"),
        "sec.52w_high": supported("RMB 32.99", source_type="market_data",
                                  url="https://example.com/quote", title="Quote",
                                  quote="52周最高 32.99"),
        "sec.align_index": derived(
            "Align",
            f"difference {market['align_index']['difference_pp']:.2f} pp <= 15 pp",
            ["co.market_cap"]),
        "sec.align_peers": derived(
            "Align",
            f"difference {market['align_peers']['difference_pp']:.2f} pp <= 15 pp",
            ["co.market_cap"]),
        "sec.significant_drop": derived("No", "max drawdown < 30%", ["co.market_cap"]),
        "sec.drop_details": not_applicable(quote="无重大下跌，不适用"),
        "sec.securities_offerings": derived("No", "no offerings in past 12 months",
                                            ["co.annual_revenue"]),
        "sec.offerings_details": not_applicable(quote="无证券发行，不适用"),
        "sec.charts": derived("Two charts generated", "market data normalised to 100",
                              ["co.market_cap"]),
        # Governance
        "cg.management_highlights": supported("Chairman and CEO have 20+ years in IT.",
                                              quote="董事长及总经理履历"),
        "cg.board_structure": supported(
            "9 directors, 3 independent; Audit and Compensation committees.",
            quote="董事会由9名董事组成"),
        "cg.director_changes": supported("No changes in the past year.", quote="董事无变动"),
        # Shareholders & employees
        "sh.major_shareholders": supported(
            "Inspur Group 32.0% (insider); others < 5%.", quote="前十大股东"),
        "sh.major_change_12m": supported("No", quote="控股股东未发生变化"),
        "sh.change_details": not_applicable(quote="无变化，不适用"),
        "emp.headcount": supported({"prc": "6,000", "usa": "40", "europe": "20",
                                    "row": "140", "total": "6,200"}, quote="员工总数 6,200 人"),
        # Audit
        "au.auditor": supported("Deloitte Touche Tohmatsu", quote="德勤华永会计师事务所"),
        "au.opinion": supported("Unqualified", quote="标准无保留意见"),
        "au.qualified_details": not_applicable(quote="非保留意见，不适用"),
        "au.auditor_change": supported("No", quote="近两年未更换审计师"),
        "au.auditor_change_details": not_applicable(quote="未更换，不适用"),
        "au.restatement": supported("No", quote="未发生财务重述"),
        "au.restatement_details": not_applicable(quote="无重述，不适用"),
        # Non-financial changes
        "nf.board_change": supported("No", quote="董事会结构无重大变化"),
        "nf.business_change": supported("No", quote="主营业务无重大变化"),
        "nf.top3_shareholder_change": supported("No", quote="前三大股东无变化"),
        "nf.details": not_applicable(quote="无变化，不适用"),
        # Litigation & news
        "lit.pending": supported("No", quote="无重大未决诉讼"),
        "lit.regulatory": supported("No", quote="无监管处罚"),
        "lit.details": not_applicable(quote="无诉讼，不适用"),
        "news.negative_news": supported(
            "2025-08-14 | Example News | Supply chain pressure report | Full text unavailable",
            source_type="media", url="https://example.com/news/1", title="Example News",
            quote="Headline only"),
    }

    for period in ("prev", "curr"):
        block = liq[period]
        fields[f"liq.current_ratio_{period}"] = derived(
            f"{block['current_ratio']:.2f}", "current assets / current liabilities",
            ["co.total_asset"])
        fields[f"liq.quick_ratio_{period}"] = derived(
            f"{block['quick_ratio']:.2f}", "(current assets - inventory) / current liabilities",
            ["co.total_asset"])
        fields[f"liq.capex_{period}"] = derived(f"{block['capex']:,.0f}", "CAPEX",
                                                ["co.total_asset"])
        fields[f"liq.capex_to_revenue_{period}"] = derived(
            f"{block['capex_to_revenue']:.2%}", "CAPEX / revenue", ["co.annual_revenue"])
        fields[f"liq.interest_coverage_{period}"] = derived(
            f"{block['interest_coverage']:.2f}",
            "(operating profit + interest expense) / interest expense", ["co.total_asset"])
        fields[f"liq.debt_to_asset_{period}"] = derived(
            f"{block['debt_to_asset']:.2f}", "total liabilities / total assets", ["co.total_asset"])
        fields[f"liq.ocf_positive_{period}"] = derived(
            "Yes" if block["ocf_positive"] else "No", "OCF > 0", ["co.total_asset"])
        fields[f"liq.cashflow_gt_noi_{period}"] = derived(
            "Yes" if block["cash_flow_gt_noi"] else "No", "OCF > net income",
            ["co.annual_net_profit"])

    return {"target": TARGET, "peers": PEERS, "as_of_date": "2026-09-20", "fields": fields}


def gaps() -> dict:
    return {"gaps": [{
        "field_id": "us.revenue",
        "reason": "US revenue is not separately disclosed in the annual report.",
        "attempts": [
            "annual report segment note: no US split",
            "exchange announcements: no US revenue filing",
        ],
    }]}


def build_run(run_dir: Path, template: Path = TEMPLATE, render: bool = False) -> dict:
    from calculate_metrics import compute
    from write_report import prepare, write

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run-plan.json").write_text(
        json.dumps(run_plan(), ensure_ascii=False, indent=2), encoding="utf-8")

    prepare(str(template), str(run_dir / "working-template.docx"),
            str(run_dir / "template-manifest.json"), expected_sha256=ORIGINAL_SHA256)

    spec = metrics_input()
    metrics = compute(spec, chart_dir=run_dir)
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    ev = evidence(metrics)
    (run_dir / "evidence.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "gaps.json").write_text(
        json.dumps(gaps(), ensure_ascii=False, indent=2), encoding="utf-8")

    write(str(run_dir / "working-template.docx"), str(run_dir / "evidence.json"),
          str(run_dir / "result.docx"), charts=str(run_dir))

    from audit_report import run_audit

    audit = run_audit(run_dir, template, expected_sha=ORIGINAL_SHA256, render=render)
    report = {"passed": audit.passed, "errors": audit.errors,
              "warnings": audit.warnings, "checks": audit.checks}
    (run_dir / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
