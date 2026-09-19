from datetime import date
import json

import httpx
import pytest

from mlc_agent.cninfo import AnnouncementDocument
from mlc_agent.production_adapters import collect_shared_disclosures
from mlc_agent.report_parser import ParsedReport
from mlc_agent.schemas import CompanyIdentity
from mlc_agent.szse import SZSE_ANNOUNCEMENT_API, fetch_szse_announcements


def _sz_company(code: str = "000938") -> CompanyIdentity:
    return CompanyIdentity(
        company_name="紫光股份",
        company_short_name="紫光股份",
        stock_code=code,
        exchange="深圳证券交易所",
        eastmoney_secid=f"0.{code}",
        eastmoney_secu_code=f"{code}.SZ",
        xueqiu_symbol=f"SZ{code}",
    )


def test_szse_announcement_adapter_preserves_official_pdf_and_source():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(SZSE_ANNOUNCEMENT_API)
        assert request.headers["referer"] == "https://www.szse.cn/disclosure/listed/notice/index.html"
        assert json.loads(request.content) == {
            "stock": ["000938"],
            "seDate": ["2025-01-01", "2026-07-03"],
            "channelCode": ["listedNotice_disc"],
            "pageSize": 30,
            "pageNum": 1,
        }
        return httpx.Response(
            200,
            json={
                "data": [{
                    "id": "szse-1",
                    "title": "2025年年度报告",
                    "publishTime": "2026-04-20 00:00:00",
                    "attachPath": "/disc/disk01/finalpage/2026-04-20/report.PDF",
                    "secCode": ["000938"],
                }],
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        documents = fetch_szse_announcements(
            client,
            stock_code="000938",
            start_date=date(2025, 1, 1),
            end_date=date(2026, 7, 3),
        )

    assert documents[0].source == "exchange"
    assert documents[0].document_type == "annual_report"
    assert documents[0].url == "https://disc.static.szse.cn/download/disc/disk01/finalpage/2026-04-20/report.PDF"


def test_szse_timestamp_with_timezone_uses_china_calendar_date():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{
                    "id": "utc",
                    "title": "董事变更公告",
                    "publishTime": "2026-09-15T16:00:00Z",
                    "attachPath": "notice.pdf",
                    "secCode": ["000938"],
                }],
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        documents = fetch_szse_announcements(
            client,
            stock_code="000938",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 19),
        )

    assert documents[0].published_at.date() == date(2026, 9, 16)


def test_shared_disclosures_uses_szse_without_cninfo_org_lookup(monkeypatch, tmp_path):
    document = AnnouncementDocument(
        announcement_id="szse-1",
        stock_code="000938",
        title="2025年年度报告",
        published_at="2026-04-20T00:00:00",
        url="https://disc.static.szse.cn/download/report.pdf",
        document_type="annual_report",
        report_year=2025,
        source="exchange",
    )
    monkeypatch.setattr(
        "mlc_agent.production_adapters.fetch_szse_announcements",
        lambda *args, **kwargs: [document],
    )
    monkeypatch.setattr(
        "mlc_agent.production_adapters.discover_cninfo_org_id",
        lambda *args, **kwargs: pytest.fail("SZSE collection must not call CNINFO org lookup"),
    )
    monkeypatch.setattr(
        "mlc_agent.production_adapters.download_announcement",
        lambda *args, **kwargs: b"pdf",
    )
    monkeypatch.setattr(
        "mlc_agent.production_adapters.parse_machine_generated_pdf",
        lambda path: ParsedReport(path=str(path), pages=[]),
    )

    result = collect_shared_disclosures(
        object(), company=_sz_company(), as_of=date(2026, 7, 3), run_dir=tmp_path
    )

    assert result.org_id is None
    assert result.catalog_source == "exchange"
    assert result.catalog_source_url == "https://www.szse.cn/disclosure/listed/notice/index.html"
    assert result.announcement_catalog[0].source == "exchange"
