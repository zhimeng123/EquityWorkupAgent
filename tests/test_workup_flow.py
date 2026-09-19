from datetime import datetime
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from mlc_agent import integration, nodes
from mlc_agent.config import load_source_priorities, load_template_mapping
from mlc_agent.field_merger import build_static_values
from mlc_agent.graph import (
    _create_production_execution_plan_node,
    _observable_part,
    build_workup_graph,
)
from mlc_agent.official_site import OfficialPage
from mlc_agent.schemas import CompanyIdentity, SourceValue
from mlc_agent.production_adapters import SharedDisclosureBundle
from mlc_agent.related_parties import RelatedPartyTransaction
from mlc_agent.service import run_workup


ROOT = Path(__file__).resolve().parents[1]


def test_production_graph_does_not_require_xueqiu_fetch_for_required_fields():
    graph = build_workup_graph()

    assert "fetch_xueqiu" not in graph.nodes
    assert "fetch_official_site" in graph.nodes
    plan = _create_production_execution_plan_node({"field_mapping": []})["execution_plan"]
    assert all(step["step_id"] != "fetch_xueqiu" for step in plan)

    mappings = load_template_mapping(ROOT / "configs")["fields"]
    priorities = load_source_priorities(ROOT / "configs", mappings)
    for mapping in mappings:
        non_xueqiu_sources = [
            source
            for source in priorities[mapping["field_id"]]
            if source != "xueqiu"
        ]
        assert non_xueqiu_sources, mapping["field_id"]


def test_production_graph_keeps_static_xueqiu_url_value():
    company = CompanyIdentity(
        company_name="紫光股份",
        company_short_name="紫光股份",
        stock_code="000938",
        exchange="深圳证券交易所",
        eastmoney_secid="0.000938",
        eastmoney_secu_code="000938.SZ",
        xueqiu_symbol="SZ000938",
    )

    values = build_static_values(
        company,
        ROOT / "Workup_template_260617-外测版.docx",
        datetime.fromisoformat("2026-07-03T09:00:00+00:00"),
    )

    xueqiu_url = next(item for item in values if item.field_id == "xueqiu_url")
    assert xueqiu_url.value == "https://xueqiu.com/S/SZ000938"
    assert xueqiu_url.source == "system"


class _GroupResult(BaseModel):
    marker: str


def _part05_state() -> dict:
    company = CompanyIdentity(
        company_name="紫光股份", company_short_name="紫光股份", stock_code="000938",
        exchange="深圳证券交易所", eastmoney_secid="0.000938",
        eastmoney_secu_code="000938.SZ", xueqiu_symbol="SZ000938",
    )
    annual_records = [
        {
            "report_date": f"{year}-12-31", "period_type": "annual", "fiscal_year": year,
            "revenue": "100", "parent_net_profit": "10", "gross_profit": "20",
            "source_url": f"https://example.com/{year}.pdf",
        }
        for year in (2025, 2024)
    ]
    return {
        "company": company.model_dump(mode="json"), "config_dir": "configs",
        "created_at": "2026-07-03T09:00:00+00:00",
        "part_results": {
            "part_04": {"annual_records": annual_records},
            "shared_disclosures": SharedDisclosureBundle(
                org_id="gssz0000938", documents=[], announcement_catalog=[]
            ).model_dump(mode="json"),
        },
        "source_values": [], "node_errors": [], "artifacts": [],
        "execution_plan": [
            {"step_id": "part_05", "status": "pending", "detail": None},
            {"step_id": "part_08", "status": "pending", "detail": None},
        ],
    }


def _mock_successful_peer_boundary(monkeypatch):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    peers = [
        CompanyIdentity(
            company_name=f"同行{code}", company_short_name=f"同行{code}", stock_code=code,
            exchange="深圳证券交易所", eastmoney_secid=f"0.{code}",
            eastmoney_secu_code=f"{code}.SZ", xueqiu_symbol=f"SZ{code}",
        )
        for code in ("000034", "000977", "603019")
    ]
    target = SimpleNamespace(
        annual_records=[
            {
                "report_date": "2025-12-31", "period_type": "annual", "fiscal_year": 2025,
                "revenue": "100", "parent_net_profit": "10", "gross_profit": "20",
                "source_url": "https://example.com/2025.pdf",
            },
            {
                "report_date": "2024-12-31", "period_type": "annual", "fiscal_year": 2024,
                "revenue": "90", "parent_net_profit": "9", "gross_profit": "18",
                "source_url": "https://example.com/2024.pdf",
            },
        ],
        model_copy=lambda **_kwargs: target,
    )
    selected = [SimpleNamespace(stock_code=item.stock_code) for item in peers]
    peer_values = [
        SourceValue(
            field_id=field_id, value="verified", raw_value={}, source="derived",
            source_url="https://example.com/peer", captured_at="2026-07-03T09:00:00+00:00",
        )
        for field_id in ("peer_comparison_table", "peer_alignment_comment")
    ]
    result = SimpleNamespace(
        selection=SimpleNamespace(success=True, selected=selected, failure_reason=None),
        failures=[], source_values=peer_values,
        model_dump=lambda **_kwargs: {"selection": {"success": True}},
    )
    monkeypatch.setattr(integration, "build_http_client", lambda **_kwargs: Context())
    monkeypatch.setattr(
        integration, "collect_fixed_peer_companies",
        lambda *_args, **_kwargs: (target, [], peers),
    )
    monkeypatch.setattr(integration, "collect_peer_analysis", lambda **_kwargs: result)
    return peers


def test_part05_related_party_timeout_preserves_completed_peer_contract(monkeypatch):
    _mock_successful_peer_boundary(monkeypatch)
    monkeypatch.setattr(integration, "_llm_client", lambda: object())
    monkeypatch.setattr(
        integration, "extract_part05_related_party_transactions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Request timed out")),
    )

    output = integration.part05_node(_part05_state())

    result = output["part_results"]["part_05"]
    assert result["status"] == "partial"
    assert result["peer_analysis_status"] == "completed"
    assert result["related_party_status"] == "failed"
    assert len(result["selected_identities"]) == 3
    assert {item["field_id"] for item in output["source_values"]} == {
        "peer_comparison_table", "peer_alignment_comment"
    }
    assert all(item["field_id"] != "related_party_transactions" for item in output["source_values"])
    assert output["node_errors"][-1]["message"] == (
        "related_party_transactions: Request timed out"
    )
    assert output["execution_plan"][0]["status"] == "partial"
    assert output["execution_plan"][0]["detail"] == (
        "related_party_transactions: Request timed out"
    )


def test_part05_all_boundaries_succeed(monkeypatch):
    _mock_successful_peer_boundary(monkeypatch)
    monkeypatch.setattr(integration, "_llm_client", lambda: object())
    monkeypatch.setattr(
        integration, "extract_part05_related_party_transactions",
        lambda *_args, **_kwargs: [RelatedPartyTransaction.model_validate({
                "related_party": "关联方甲", "relationship": "Affiliate",
                "transaction_type": "Sale", "period": "FY2025", "source": "annual_report",
                "source_url": "https://example.com/annual.pdf",
            })],
    )

    output = integration.part05_node(_part05_state())

    result = output["part_results"]["part_05"]
    assert result["status"] == "completed"
    assert result["peer_analysis_status"] == "completed"
    assert result["related_party_status"] == "completed"
    assert {item["field_id"] for item in output["source_values"]} == {
        "peer_comparison_table", "peer_alignment_comment", "related_party_transactions"
    }
    assert output["node_errors"] == []


def test_part05_peer_analysis_survives_shared_disclosure_failure(monkeypatch):
    _mock_successful_peer_boundary(monkeypatch)
    state = _part05_state()
    state["part_results"]["shared_disclosures"] = {
        "status": "failed",
        "reason": "CNINFO company search returned HTTP 403",
    }
    monkeypatch.setattr(integration, "_llm_client", lambda: object())
    monkeypatch.setattr(
        integration,
        "extract_part05_related_party_transactions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("shared disclosures unavailable")),
    )

    output = integration.part05_node(state)

    result = output["part_results"]["part_05"]
    assert result["status"] == "partial"
    assert result["peer_analysis_status"] == "completed"
    assert result["related_party_status"] == "failed"
    assert {item["field_id"] for item in output["source_values"]} == {
        "peer_comparison_table", "peer_alignment_comment",
    }


def test_part05_peer_failure_blocks_part08_without_running_its_llm(monkeypatch):
    state = _part05_state()
    state["part_results"]["part_05"] = {
        "status": "failed", "reason": "fixed peer financial collection failed"
    }
    monkeypatch.setattr(
        integration, "extract_pydantic",
        lambda *_args, **_kwargs: pytest.fail("Part 08 LLM must not run after peer failure"),
    )

    output = integration.part08_node(state)

    assert output["part_results"]["part_08"] == {
        "status": "failed",
        "reason": "Part 05 peer analysis failed: fixed peer financial collection failed",
    }


def test_part08_accepts_partial_part05_when_peer_contract_is_completed(monkeypatch):
    state = _part05_state()
    peers = _mock_successful_peer_boundary(monkeypatch)
    state["part_results"]["part_05"] = {
        "status": "partial", "peer_analysis_status": "completed",
        "related_party_status": "failed", "related_party_error": "Request timed out",
        "selected_identities": [item.model_dump(mode="json") for item in peers],
    }
    monkeypatch.setattr(integration, "_llm_client", lambda: object())
    monkeypatch.setattr(
        integration, "extract_pydantic",
        lambda *_args, **_kwargs: SimpleNamespace(ipo_candidates=[], offering_review=object()),
    )
    history = object()
    monkeypatch.setattr(integration, "market_history", lambda *_args, **_kwargs: history)
    security_result = SimpleNamespace(
        source_values=[], errors=[],
        model_dump=lambda **_kwargs: {"status": "completed", "market_history": {}},
    )
    monkeypatch.setattr(
        integration, "collect_security_analysis",
        lambda **kwargs: security_result if kwargs["market"] is history else None,
    )

    output = integration.part08_node(state)

    assert output["part_results"]["part_08"]["status"] == "completed"


def test_part11_runs_isolated_groups_and_preserves_success_when_one_times_out(monkeypatch, tmp_path):
    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    bundle = SharedDisclosureBundle(org_id="gssz0000938", documents=[], announcement_catalog=[])
    monkeypatch.setattr(integration, "_bundle", lambda _state: bundle)
    monkeypatch.setattr(integration, "build_http_client", lambda **_kwargs: Context())
    monkeypatch.setattr(integration, "discover_negative_news", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(integration, "_llm_client", lambda: object())
    groups = []

    def extract(*_args, output_model, **_kwargs):
        groups.append(output_model.__name__)
        if output_model.__name__ == "Part11AuditExtraction":
            raise TimeoutError("Request timed out")
        return _GroupResult(marker=output_model.__name__)

    monkeypatch.setattr(integration, "extract_pydantic", extract)
    state = {
        "company": CompanyIdentity(
            company_name="紫光股份", company_short_name="紫光股份", stock_code="000938",
            exchange="深圳证券交易所", eastmoney_secid="0.000938",
            eastmoney_secu_code="000938.SZ", xueqiu_symbol="SZ000938",
        ).model_dump(mode="json"),
        "created_at": "2026-07-03T09:00:00+00:00",
        "run_dir": str(tmp_path),
        "part_results": {}, "source_values": [], "node_errors": [], "artifacts": [],
        "execution_plan": [{"step_id": "part_11", "status": "pending", "detail": None}],
    }
    output = integration.part11_node(state)
    assert groups == [
        "Part11AuditExtraction", "Part11RestatementExtraction",
        "Part11BoardChangesExtraction", "Part11BusinessChangesExtraction",
        "Part11ShareholderChangesExtraction", "Part11LitigationExtraction",
        "Part11RegulatoryExtraction", "Part11NewsExtraction",
    ]
    result = output["part_results"]["part_11"]
    assert result["status"] == "partial"
    assert set(result["groups"]) == {
        "restatements", "board_changes", "business_changes",
        "shareholder_changes", "litigation", "regulatory", "news",
    }
    assert result["failed_groups"] == ["audit: Request timed out"]


def test_part_observability_logs_start_finish_and_elapsed_time(caplog):
    with caplog.at_level(logging.INFO, logger="mlc_agent"):
        output = _observable_part("part_test", lambda state: {"value": state["value"]})(
            {"value": 1}
        )

    assert output == {"value": 1}
    messages = [record.getMessage() for record in caplog.records]
    assert "Node part_test started" in messages
    assert any(message.startswith("Node part_test finished in ") for message in messages)


@pytest.mark.parametrize(
    ("part", "node"),
    [
        ("part_01", integration.part01_node),
        ("part_03", integration.part03_node),
        ("part_04", integration.part04_node),
        ("part_06", integration.part06_node),
        ("part_07", integration.part07_node),
        ("part_10", integration.part10_node),
        ("part_11", integration.part11_node),
    ],
)
def test_shared_disclosure_failure_propagates_without_model_validation_noise(part, node):
    state = {
        "part_results": {
            "shared_disclosures": {
                "status": "failed",
                "reason": "CNINFO company search returned HTTP 500",
            }
        },
        "source_values": [],
        "node_errors": [],
        "artifacts": [],
        "execution_plan": [
            {"step_id": part, "status": "pending", "detail": None}
        ],
    }

    output = node(state)

    result = output["part_results"][part]
    assert result == {
        "status": "failed",
        "reason": (
            "shared disclosures failed: "
            "CNINFO company search returned HTTP 500"
        ),
    }
    assert "validation error" not in result["reason"].lower()
    assert output["execution_plan"][0]["status"] == "failed"


def test_failure_reason_uses_field_specific_upstream_error():
    reasons = nodes._failure_reasons(
        {
            "config_dir": "configs",
            "field_mapping": [{"field_id": "business_description"}],
            "errors": [
                {
                    "node": "summarize_business_description",
                    "message": "LLM returned HTTP 401",
                }
            ],
            "node_errors": [],
            "part_results": {},
        }
    )

    assert reasons["business_description"] == (
        "summarize_business_description: LLM returned HTTP 401"
    )


def test_part09_raw_status_matches_execution_plan_when_chart_inputs_fail():
    state = {
        "part_results": {"part_08": {}},
        "source_values": [],
        "artifacts": [],
        "node_errors": [],
        "run_dir": "/tmp",
        "created_at": "2026-07-03T09:00:00+00:00",
        "execution_plan": [{"step_id": "part_09", "status": "pending", "detail": None}],
    }

    output = integration.part09_node(state)

    assert output["part_results"]["part_09"]["status"] == "failed"
    assert output["execution_plan"][0]["status"] == "failed"


def test_langgraph_full_flow_preserves_mvp1_while_new_parts_fail_explicitly(monkeypatch, tmp_path):
    company = CompanyIdentity(
        company_name="紫光股份",
        company_short_name="紫光股份",
        stock_code="000938",
        exchange="深圳证券交易所",
        eastmoney_secid="0.000938",
        eastmoney_secu_code="000938.SZ",
        xueqiu_symbol="SZ000938",
    )
    profile = {
        "ORG_NAME": "紫光股份有限公司",
        "SECURITY_NAME_ABBR": "紫光股份",
        "ORG_NAME_EN": "Unisplendour Corporation Limited",
        "TRADE_MARKET": "深圳证券交易所",
        "ORG_WEB": "www.thunis.com",
        "ORG_PROFILE": "紫光股份提供信息通信基础设施和数字化解决方案。" * 10,
        "BUSINESS_SCOPE": "信息电子及相关产业。",
        "MAIN_BUSINESS": "信息电子及相关产业。",
        "FOUND_DATE": "1999-03-18 00:00:00",
        "LISTING_DATE": "1999-11-04 00:00:00",
    }
    financials = {
        "latest": {
            "REPORT_DATE": "2026-03-31 00:00:00",
            "REPORT_DATE_NAME": "2026一季报",
            "REPORT_YEAR": "2026",
            "TOTAL_ASSETS_PK": 103_000_000_000,
            "TOTAL_EQUITY_PK": 18_000_000_000,
            "TOTAL_SHARE": 2_860_000_000,
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
    eastmoney_market = {
        "quote_date": "2026-07-02",
        "current": 29.03,
        "market_capital": 83_000_000_000,
        "high52w": 34.43,
        "low52w": 23.17,
        "total_shares": 2_860_000_000,
    }
    xueqiu_market = {
        "current": 29.03,
        "market_capital": 83_000_000_000,
        "high52w": 34.43,
        "low52w": 23.17,
    }

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr("mlc_agent.nodes.resolve_a_share_company", lambda client, value: company)
    monkeypatch.setattr("mlc_agent.nodes.fetch_company_profile", lambda client, value: profile)
    monkeypatch.setattr("mlc_agent.nodes.fetch_financial_summary", lambda client, value: financials)
    monkeypatch.setattr("mlc_agent.nodes.fetch_financial_period_records", lambda client, value: [])
    monkeypatch.setattr(
        "mlc_agent.nodes.fetch_market_snapshot",
        lambda client, value, total_shares: eastmoney_market,
    )

    def completed_without_candidates(step_id):
        def node(state):
            plan = [dict(item) for item in state["execution_plan"]]
            for item in plan:
                if item["step_id"] == step_id:
                    item["status"] = "completed"
                    item["detail"] = "integration fixture: no verified candidates"
            return {"execution_plan": plan}
        return node

    for name in (
        "shared_disclosures",
        "part_01", "part_02", "part_03", "part_04", "part_05", "part_06",
        "part_07", "part_08", "part_09", "part_10", "part_11",
    ):
        attribute = "collect_shared_disclosures_node" if name == "shared_disclosures" else name.replace("_", "") + "_node"
        monkeypatch.setattr(f"mlc_agent.graph.{attribute}", completed_without_candidates(name))
    monkeypatch.setattr("mlc_agent.nodes.fetch_xueqiu_snapshot", lambda client, value: xueqiu_market)
    monkeypatch.setattr(
        "mlc_agent.nodes.fetch_official_profile",
        lambda client, url: OfficialPage(
            url="https://www.thunis.com/about",
            text="Official verified company profile text. " * 20,
        ),
    )
    monkeypatch.setattr(
        "mlc_agent.nodes.summarize_business_description",
        lambda *args, **kwargs: (
            "Founded in 1999 and listed in Shenzhen, Unisplendour Corporation Limited provides ICT "
            "infrastructure and digital solutions, including networking, servers, storage, cybersecurity, "
            "cloud computing, and intelligent terminals."
        ),
    )

    result = run_workup(
        template=ROOT / "Workup_template_260617-外测版.docx",
        company="000938",
        output=tmp_path,
        goal="Generate workup",
        auto_confirm=True,
        debug=False,
        keep_intermediate=False,
    )

    assert result["self_check_result"]["passed"] is False
    assert len(result["field_results"]) == 16
    assert len(result["failed_fields"]) == len(result["field_mapping"]) - 16
    assert all(item["reason"] for item in result["failed_fields"])
    assert Path(result["output_docx_path"]).exists()
    assert Path(result["sources_json_path"]).exists()
    assert next(
        step for step in result["execution_plan"] if step["step_id"] == "self_check"
    )["status"] == "failed"
