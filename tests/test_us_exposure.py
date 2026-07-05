from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
from docx import Document

from mlc_agent.config import load_template_mapping
from mlc_agent.docx_writer import validate_template_mapping, write_docx_by_mapping
from mlc_agent.us_exposure import (
    EvidenceDocument,
    PART_03_FIELD_IDS,
    collect_us_exposure,
    make_openai_extractor,
    us_exposure_node,
)


URL = "https://static.cninfo.com.cn/2025-annual.pdf"
PERIOD = "FY2025"
AS_OF = date(2026, 7, 3)
CAPTURED = datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc)
TEXT = (
    "公司在美国设有Alpha US Inc.，注册于美国，主要从事软件销售。"
    "公司在Texas设有Beta LLC，主要从事技术服务。"
    "公司在德国设有German GmbH，并披露其他境外业务收入。"
    "报告覆盖境外业务，但未单列其他美国敞口。"
    "美国地区营业收入为200万美元。公司营业总收入为1000万美元。"
    "美国员工共12人，其中California 7人、Texas 5人。"
    "公司明确披露不存在美国子公司，美国子公司数量为0。"
    "美国地区营业收入明确为0美元。美国员工人数明确为0人。"
    "报告覆盖子公司、地区收入及员工主题，但未单列美国数据。"
)


@pytest.fixture
def documents() -> list[EvidenceDocument]:
    return [EvidenceDocument(
        source="annual_report",
        source_url=URL,
        disclosure_date=date(2026, 4, 20),
        period=PERIOD,
        text=TEXT,
    )]


def ev(text: str, *, period: str = PERIOD) -> dict[str, object]:
    return {
        "source_url": URL,
        "disclosure_date": "2026-04-20",
        "period": period,
        "page_number": 1,
        "evidence_text": text,
    }


def status(value: str, text: str, *, period: str = PERIOD) -> dict[str, object]:
    return {"status": value, "evidence": ev(text, period=period)}


def coverage_status(*, period: str = PERIOD) -> dict[str, object]:
    return {
        "status": "not_disclosed",
        "coverage_evidence": {
            "source_url": URL,
            "disclosure_date": "2026-04-20",
            "period": period,
            "page_numbers": [1],
            "sections": ["子公司", "分地区收入", "员工"],
            "search_dimensions": ["美国", "United States", "USA"],
        },
    }


def payload() -> dict[str, object]:
    return {
        "subsidiaries": {
            "status": status("yes", "公司在美国设有Alpha US Inc.，注册于美国，主要从事软件销售。"),
            "count": 2,
            "subsidiaries": [
                {
                    "name": "Alpha US Inc.",
                    "registered_location": "美国",
                    "country": "美国",
                    "principal_business": "软件销售",
                    "report_period": PERIOD,
                    "evidence": ev("公司在美国设有Alpha US Inc.，注册于美国，主要从事软件销售。"),
                },
                {
                    "name": "Beta LLC",
                    "registered_location": "Texas",
                    "us_state": "Texas",
                    "principal_business": "技术服务",
                    "report_period": PERIOD,
                    "evidence": ev("公司在Texas设有Beta LLC，主要从事技术服务。"),
                },
            ],
        },
        "other_details": {
            "status": coverage_status(),
            "details": [],
        },
        "us_revenue": {
            "status": status("yes", "美国地区营业收入为200万美元。"),
            "amount": "200",
            "currency": "USD million",
            "period": PERIOD,
            "evidence": ev("美国地区营业收入为200万美元。"),
            "geographic_scope": "United States",
        },
        "total_revenue": {
            "status": status("yes", "公司营业总收入为1000万美元。"),
            "amount": "1000",
            "currency": "USD million",
            "period": PERIOD,
            "evidence": ev("公司营业总收入为1000万美元。"),
        },
        "us_employees": {
            "status": status("yes", "美国员工共12人，其中California 7人、Texas 5人。"),
            "count": 12,
            "period": PERIOD,
            "evidence": ev("美国员工共12人，其中California 7人、Texas 5人。"),
        },
        "employee_by_state": {
            "status": status("yes", "美国员工共12人，其中California 7人、Texas 5人。"),
            "items": [
                {"state": "California", "count": 7, "evidence": ev("美国员工共12人，其中California 7人、Texas 5人。")},
                {"state": "Texas", "count": 5, "evidence": ev("美国员工共12人，其中California 7人、Texas 5人。")},
            ],
        },
    }


def test_deepseek_extractor_uses_plain_json_instruction_and_local_validation(documents):
    class FakeCompletions:
        def create(self, **kwargs):
            assert "response_format" not in kwargs
            assert "JSON Schema" in kwargs["messages"][0]["content"]
            assert "status MUST be a nested JSON object" in kwargs["messages"][0]["content"]
            request = json.loads(kwargs["messages"][1]["content"])
            message = type("Message", (), {"content": json.dumps(payload()[request["field"]])})()
            return type("Response", (), {"choices": [type("Choice", (), {"message": message})()]})()

    client = type("Client", (), {"chat": type("Chat", (), {"completions": FakeCompletions()})()})()

    result = make_openai_extractor(client, model="deepseek-chat")(documents, as_of=AS_OF)

    assert result["subsidiaries"]["count"] == 2


def test_deepseek_extractor_rebuilds_every_not_disclosed_coverage_from_supplied_pages():
    document = EvidenceDocument(
        source="annual_report",
        source_url=URL,
        disclosure_date=date(2026, 4, 20),
        period=PERIOD,
        text="[Page 7]\n子公司及境外业务\n[Page 9]\n分地区收入与员工情况",
    )

    class FakeCompletions:
        def create(self, **kwargs):
            request = json.loads(kwargs["messages"][1]["content"])
            key = request["field"]
            shapes = {
                "subsidiaries": {"count": None, "subsidiaries": []},
                "other_details": {"details": []},
                "us_revenue": {},
                "total_revenue": {},
                "us_employees": {},
                "employee_by_state": {"items": []},
            }
            result = {
                "status": {
                    "status": "not_disclosed",
                    "coverage_evidence": {
                        "source_url": "https://invented.invalid/report.pdf",
                        "disclosure_date": "2020-01-01",
                        "period": "FY2019",
                        "page_numbers": [999],
                        "sections": ["invented"],
                        "search_dimensions": ["invented"],
                    },
                },
                **shapes[key],
            }
            message = type("Message", (), {"content": json.dumps(result)})()
            return type("Response", (), {"choices": [type("Choice", (), {"message": message})()]})()

    client = type("Client", (), {"chat": type("Chat", (), {"completions": FakeCompletions()})()})()
    extracted = make_openai_extractor(client, model="deepseek-chat")([document], as_of=AS_OF)

    for key in (
        "subsidiaries", "other_details", "us_revenue", "total_revenue",
        "us_employees", "employee_by_state",
    ):
        coverage = extracted[key]["status"]["coverage_evidence"]
        assert coverage["source_url"] == URL
        assert coverage["disclosure_date"] == "2026-04-20"
        assert coverage["period"] == PERIOD
        assert coverage["page_numbers"] == [7, 9]
        assert coverage["sections"] != ["invented"]
        assert coverage["search_dimensions"] != ["invented"]

    result = collect([document], extracted)
    assert not result.errors
    assert {item.field_id for item in result.source_values} == set(PART_03_FIELD_IDS)


def collect(documents, data):
    return collect_us_exposure(
        documents,
        as_of=AS_OF,
        extractor=lambda _documents, as_of: data,
        captured_at=CAPTURED,
    )


def test_mixed_us_non_us_and_vague_overseas_only_keeps_explicit_us(documents):
    result = collect(documents, payload())
    assert not result.errors
    details = next(item for item in result.source_values if item.field_id == "us_subsidiary_details")
    assert "Alpha US Inc." in details.value and "Beta LLC" in details.value
    assert "German GmbH" not in details.value and "境外" not in details.value
    assert len(details.item_evidence) == 2
    assert all(item.source_url == URL for item in details.item_evidence)


def test_non_us_jurisdiction_is_rejected(documents):
    data = payload()
    data["subsidiaries"]["subsidiaries"][0]["country"] = None
    data["subsidiaries"]["subsidiaries"][0]["us_state"] = "Bavaria"
    result = collect(documents, data)
    assert result.source_values
    assert {item.field_id for item in result.errors} == {
        "us_subsidiary_status", "us_subsidiary_count", "us_subsidiary_details"
    }
    assert "explicit US country" in result.errors[0].reason


def test_invalid_total_revenue_rejects_ratio_only_and_preserves_other_blocks(documents):
    data = payload()
    data["total_revenue"]["evidence"] = None
    result = collect(documents, data)

    assert {item.field_id for item in result.source_values} == {
        "us_subsidiary_status", "us_subsidiary_count", "us_subsidiary_details",
        "us_exposure_other_details", "us_revenue", "us_employee_count", "us_employee_by_state",
    }
    assert {item.field_id for item in result.errors} == {"us_revenue_ratio"}


def test_same_period_revenue_ratio_has_two_input_derived_evidence(documents):
    result = collect(documents, payload())
    ratio = next(item for item in result.source_values if item.field_id == "us_revenue_ratio")
    assert ratio.value == "20.00%"
    assert ratio.source == "derived"
    assert ratio.derived_evidence.formula == "US revenue / total revenue × 100%"
    assert [item.field_id for item in ratio.derived_evidence.inputs] == ["us_revenue", "total_revenue"]
    assert all(item.period == PERIOD and item.source_url == URL for item in ratio.derived_evidence.inputs)


def test_mismatched_revenue_period_rejects_ratio_only(documents):
    data = payload()
    data["total_revenue"]["period"] = "FY2024"
    data["total_revenue"]["evidence"]["period"] = "FY2024"
    # A second period-specific report makes both source facts individually valid.
    older = documents[0].model_copy(update={"period": "FY2024"})
    result = collect([documents[0], older], data)
    assert len(result.source_values) == 7
    assert result.errors[0].field_id == "us_revenue_ratio"
    assert "periods do not match" in result.errors[0].reason


def test_mismatched_revenue_units_reject_ratio_only(documents):
    data = payload()
    data["total_revenue"]["currency"] = "CNY million"
    result = collect(documents, data)
    assert len(result.source_values) == 7
    assert result.errors[0].field_id == "us_revenue_ratio"
    assert "currencies or units" in result.errors[0].reason


def test_unverifiable_total_revenue_evidence_fails_ratio_only(documents):
    data = payload()
    data["total_revenue"]["evidence"]["evidence_text"] = "报告中不存在的总收入证据"
    result = collect(documents, data)
    assert len(result.source_values) == 7
    assert len(result.errors) == 1
    assert result.errors[0].field_id == "us_revenue_ratio"
    assert "could not be anchored" in result.errors[0].reason


def test_state_breakdown_period_mismatch_fails_state_field_only(documents):
    data = payload()
    data["employee_by_state"]["items"][0]["evidence"]["period"] = "FY2024"
    older = documents[0].model_copy(update={"period": "FY2024"})
    result = collect([documents[0], older], data)
    assert len(result.source_values) == 7
    assert len(result.errors) == 1
    assert result.errors[0].field_id == "us_employee_by_state"
    assert "does not match employee total period" in result.errors[0].reason


def test_explicit_zero_is_no_and_zero_not_missing(documents):
    data = payload()
    data["subsidiaries"] = {
        "status": status("no", "公司明确披露不存在美国子公司，美国子公司数量为0。"),
        "count": 0,
        "subsidiaries": [],
    }
    data["us_revenue"] = {
        "status": status("no", "美国地区营业收入明确为0美元。"),
        "amount": "0", "currency": "USD million", "period": PERIOD,
        "evidence": ev("美国地区营业收入明确为0美元。"),
        "geographic_scope": "United States",
    }
    data["us_employees"] = {
        "status": status("no", "美国员工人数明确为0人。"),
        "count": 0, "period": PERIOD, "evidence": ev("美国员工人数明确为0人。"),
    }
    data["employee_by_state"] = {
        "status": status("no", "美国员工人数明确为0人。"), "items": []
    }
    result = collect(documents, data)
    values = {item.field_id: item.value for item in result.source_values}
    assert values["us_subsidiary_status"] == "No"
    assert values["us_subsidiary_count"] == "0"
    assert values["us_revenue"] == "USD million 0"
    assert values["us_revenue_ratio"] == "0.00%"
    assert values["us_employee_count"] == "0"


def test_not_disclosed_is_distinct_from_zero_and_ratio_is_verified_unknown(documents):
    data = payload()
    data["subsidiaries"] = {
        "status": coverage_status(), "count": None, "subsidiaries": []
    }
    data["us_revenue"] = {"status": coverage_status()}
    data["us_employees"] = {"status": coverage_status()}
    data["employee_by_state"] = {"status": coverage_status(), "items": []}
    result = collect(documents, data)
    values = {item.field_id: item.value for item in result.source_values}
    assert values["us_subsidiary_status"] == "Not separately disclosed"
    assert values["us_subsidiary_count"] == "Not separately disclosed"
    assert values["us_revenue"] == "Not separately disclosed"
    assert values["us_employee_count"] == "Not separately disclosed"
    assert values["us_revenue_ratio"] == "Not separately disclosed"
    assert not result.errors


def test_generic_foreign_revenue_is_not_accepted_as_us_revenue(documents):
    data = payload()
    data["us_revenue"]["status"] = status("yes", "公司在德国设有German GmbH，并披露其他境外业务收入。")
    data["us_revenue"]["evidence"] = ev("公司在德国设有German GmbH，并披露其他境外业务收入。")
    result = collect(documents, data)
    assert "us_revenue" not in {item.field_id for item in result.source_values}
    error = next(item for item in result.errors if item.field_id == "us_revenue")
    assert "foreign/overseas revenue is not US revenue" in error.reason


def test_not_disclosed_coverage_rejects_page_outside_supplied_report(documents):
    data = payload()
    data["us_revenue"] = {"status": coverage_status()}
    data["us_revenue"]["status"]["coverage_evidence"]["page_numbers"] = [99]
    result = collect(documents, data)
    assert "us_revenue" not in {item.field_id for item in result.source_values}
    assert any(
        item.field_id == "us_revenue" and "coverage pages were not supplied" in item.reason
        for item in result.errors
    )


def test_no_requires_exact_fact_evidence_not_coverage(documents):
    data = payload()
    data["us_revenue"] = {
        "status": {**coverage_status(), "status": "no"},
        "amount": "0",
        "currency": "USD million",
        "period": PERIOD,
        "evidence": ev("美国地区营业收入明确为0美元。"),
        "geographic_scope": "United States",
    }
    result = collect(documents, data)
    assert "us_revenue" not in {item.field_id for item in result.source_values}
    assert any("yes/no status requires exact fact evidence only" in item.reason for item in result.errors)


def test_unavailable_report_fails_all_fields_without_calling_extractor():
    called = False
    def extractor(_documents, *, as_of):
        nonlocal called
        called = True
        return payload()
    result = collect_us_exposure([], as_of=AS_OF, extractor=extractor, captured_at=CAPTURED)
    assert not called
    assert not result.source_values and len(result.errors) == 8


def test_multiple_subsidiaries_and_states_render_multiline(documents):
    result = collect(documents, payload())
    values = {item.field_id: item.value for item in result.source_values}
    assert values["us_subsidiary_details"].count("\n") == 1
    assert values["us_employee_by_state"] == "California: 7\nTexas: 5"


def test_node_returns_public_contract_without_word_write(documents):
    update = us_exposure_node(
        {"part_03_documents": [item.model_dump(mode="json") for item in documents]},
        extractor=lambda _documents, as_of: payload(),
        as_of=AS_OF,
    )
    assert set(update) == {"part_results", "source_values", "node_errors"}
    assert len(update["source_values"]) == 8


def test_mapping_matches_template_and_failed_fields_preserve_targets(tmp_path):
    root = Path(__file__).resolve().parents[1]
    mapping = load_template_mapping(root / "configs")
    fields = [item for item in mapping["fields"] if item["field_id"] in {
        "us_subsidiary_status", "us_subsidiary_count", "us_subsidiary_details",
        "us_exposure_other_details", "us_revenue", "us_revenue_ratio",
        "us_employee_count", "us_employee_by_state",
    }]
    assert len(fields) == 8
    template = root / "Workup_template_260617-外测版.docx"
    validate_template_mapping(template, fields)
    output = tmp_path / "unchanged.docx"
    assert write_docx_by_mapping(template, output, fields, {}) == []
    written = Document(output)
    assert "可在最新年报和半年报上抓取" in written.tables[2].cell(0, 1).text
    assert written.tables[2].cell(2, 1).text == ""
