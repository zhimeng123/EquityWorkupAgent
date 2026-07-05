from datetime import datetime

from mlc_agent.annual_report_validation import build_local_annual_bundle
from mlc_agent.report_parser import ParsedReport, PdfPage


def test_build_local_bundle_preserves_parsed_pages_without_network_contract():
    parsed = ParsedReport(
        path="/tmp/1225101866.pdf",
        pages=[PdfPage(page_number=33, text="主要控股参股公司分析")],
    )
    bundle = build_local_annual_bundle(
        parsed,
        stock_code="000938",
        report_year=2025,
        published_at=datetime(2026, 4, 20, 12, 0),
    )

    assert bundle.org_id == "local-offline-validation"
    assert bundle.announcement_catalog == []
    assert len(bundle.documents) == 1
    assert bundle.documents[0].document.document_type == "annual_report"
    assert bundle.documents[0].document.url == "https://local.invalid/1225101866.pdf"
    assert bundle.documents[0].parsed.pages[0].page_number == 33
