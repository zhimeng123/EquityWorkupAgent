from datetime import datetime

from mlc_agent import integration
from mlc_agent.cninfo import AnnouncementDocument
from mlc_agent.production_adapters import ParsedDisclosure, SharedDisclosureBundle
from mlc_agent.report_parser import ParsedReport, PdfPage
from mlc_agent.schemas import CompanyIdentity


class _ClientContext:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return None


def _company() -> CompanyIdentity:
    return CompanyIdentity(
        company_name="紫光股份",
        company_short_name="紫光股份",
        stock_code="000938",
        exchange="深圳证券交易所",
        eastmoney_secid="0.000938",
        eastmoney_secu_code="000938.SZ",
        xueqiu_symbol="SZ000938",
    )


def _state(tmp_path):
    return {
        "company": _company().model_dump(mode="json"),
        "created_at": "2026-07-03T09:00:00+00:00",
        "run_dir": str(tmp_path),
        "node_errors": [{"node": "earlier", "message": "keep"}],
        "part_results": {},
        "execution_plan": [
            {"step_id": "shared_disclosures", "status": "pending", "detail": None}
        ],
    }


def _parsed_disclosure() -> ParsedDisclosure:
    document = AnnouncementDocument(
        announcement_id="annual-2025",
        stock_code="000938",
        title="2025年年度报告",
        published_at=datetime(2026, 4, 20),
        url="https://example.test/annual-2025.pdf",
        document_type="annual_report",
        report_year=2025,
    )
    parsed = ParsedReport(
        path="/tmp/annual-2025.pdf",
        pages=[PdfPage(page_number=1, text="可提取的年度报告文本")],
    )
    return ParsedDisclosure(document=document, parsed=parsed)


def _mock_collection(monkeypatch, result):
    monkeypatch.setattr(integration, "build_http_client", lambda **_kwargs: _ClientContext())
    monkeypatch.setattr(
        integration,
        "collect_shared_disclosures",
        lambda *_args, **_kwargs: result,
    )


def test_partial_disclosure_parse_errors_stay_in_shared_result(monkeypatch, tmp_path):
    result = SharedDisclosureBundle(
        documents=[_parsed_disclosure()],
        announcement_catalog=[],
        errors=["扫描版公告.pdf: PDF contains no extractable text or tables"],
    )
    _mock_collection(monkeypatch, result)

    update = integration.collect_shared_disclosures_node(_state(tmp_path))

    shared = update["part_results"]["shared_disclosures"]
    assert shared["status"] == "partial"
    assert shared["errors"] == result.errors
    assert update["node_errors"] == [{"node": "earlier", "message": "keep"}]
    assert update["execution_plan"][0]["status"] == "partial"


def test_unusable_shared_disclosures_are_failed_and_raise_node_error(monkeypatch, tmp_path):
    result = SharedDisclosureBundle(
        documents=[],
        announcement_catalog=[],
        errors=["年度报告.pdf: download failed"],
    )
    _mock_collection(monkeypatch, result)

    update = integration.collect_shared_disclosures_node(_state(tmp_path))

    shared = update["part_results"]["shared_disclosures"]
    assert shared["status"] == "failed"
    assert shared["reason"] == result.errors[0]
    assert update["node_errors"][-1] == {
        "node": "shared_disclosures",
        "message": result.errors[0],
    }
    assert update["execution_plan"][0]["status"] == "failed"
