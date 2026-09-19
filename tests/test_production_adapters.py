from datetime import date, datetime
from decimal import Decimal
import json
import re
import ssl

import httpx
from pydantic import BaseModel
import pytest

from mlc_agent.cninfo import AnnouncementDocument
from mlc_agent.production_adapters import (
    ParsedDisclosure,
    Part06Extraction,
    Part07Extraction,
    Part07ReceivablesExtraction,
    SharedDisclosureBundle,
    collect_shared_disclosures,
    collect_fixed_peer_companies,
    discover_negative_news,
    evidence_text_documents,
    extract_part05_related_party_transactions,
    extract_pydantic,
    filtered_disclosure_payload,
    part05_related_party_payloads,
)
from mlc_agent.report_parser import ParsedReport, PdfPage
from mlc_agent.schemas import CompanyIdentity
from mlc_agent.evidence_text import anchor_extracted_evidence


class EvidenceOutput(BaseModel):
    source_url: str
    page_number: int


class JsonStrictTypes(BaseModel):
    event_date: date
    amount: Decimal


def test_local_evidence_anchor_requires_a_source_substring_and_copies_source_sentence():
    url = "https://static.cninfo.com.cn/report.pdf"
    output = {
        "ultimate_controller": "某市国资委",
        "evidence": {"source_url": url, "page_number": 2, "evidence_text": "最终实际控制人为某市国资委"},
    }
    anchored = anchor_extracted_evidence(output, [{
        "source_url": url,
        "pages": [{"page_number": 2, "text": "本公司最终实际控制人为某市国资委。其他内容。"}],
    }])
    assert anchored["evidence"]["evidence_text"] == "本公司最终实际控制人为某市国资委。"


def test_local_evidence_anchor_does_not_translate_or_guess_from_fact_values():
    url = "https://static.cninfo.com.cn/report.pdf"
    with pytest.raises(ValueError, match="could not be anchored"):
        anchor_extracted_evidence(
            {
                "ultimate_controller": "某市国资委",
                "evidence": {
                    "source_url": url,
                    "page_number": 2,
                    "evidence_text": "The ultimate controller is a municipal SASAC.",
                },
            },
            [{
                "source_url": url,
                "pages": [{"page_number": 2, "text": "本公司最终实际控制人为某市国资委。"}],
            }],
        )


def test_local_evidence_anchor_rejects_unrelated_claim():
    with pytest.raises(ValueError, match="could not be anchored"):
        anchor_extracted_evidence(
            {"source_url": "https://example.com/a", "page_number": 1, "evidence_text": "虚构收购"},
            [{"source_url": "https://example.com/a", "pages": [{"page_number": 1, "text": "年度财务报告。"}]}],
        )


def test_not_disclosed_coverage_requires_exact_supplied_excerpt():
    with pytest.raises(ValueError, match="could not be anchored"):
        anchor_extracted_evidence(
            {
                "status": "not_disclosed",
                "coverage_evidence": {
                    "source_url": "https://example.com/a",
                    "page_number": 1,
                    "evidence_text": "模型杜撰的覆盖说明",
                },
            },
            [{"source_url": "https://example.com/a", "pages": [{
                "page_number": 1, "text": "本节覆盖诉讼与仲裁事项。"
            }]}],
        )


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


class _Message:
    def __init__(self, content):
        self.content = content


class _Response:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"message": _Message(content)})()]


class _FakeLlm:
    def __init__(self, output):
        self.output = output
        self.calls = []
        self.chat = type("Chat", (), {})()
        self.chat.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        assert "response_format" not in kwargs
        assert "JSON Schema" in kwargs["messages"][0]["content"]
        return _Response(json.dumps(self.output))


def test_generic_schema_extractor_rejects_invented_url_and_page():
    payload = {
        "documents": [{
            "source_url": "https://static.cninfo.com.cn/report.pdf",
            "pages": [{"page_number": 1, "text": "verified"}],
        }]
    }
    accepted = extract_pydantic(
        _FakeLlm({"source_url": payload["documents"][0]["source_url"], "page_number": 1}),
        model="test",
        output_model=EvidenceOutput,
        system_prompt="extract",
        payload=payload,
    )
    assert accepted.page_number == 1
    with pytest.raises(ValueError, match="page_number"):
        extract_pydantic(
            _FakeLlm(
                {
                    "source_url": payload["documents"][0]["source_url"],
                    "page_number": "1",
                }
            ),
            model="test",
            output_model=EvidenceOutput,
            system_prompt="extract",
            payload=payload,
        )
    with pytest.raises(ValueError, match="URL was not supplied"):
        extract_pydantic(
            _FakeLlm({"source_url": "https://invented.example/report", "page_number": 1}),
            model="test",
            output_model=EvidenceOutput,
            system_prompt="extract",
            payload=payload,
        )
    with pytest.raises(ValueError, match="outside supplied document"):
        extract_pydantic(
            _FakeLlm({"source_url": payload["documents"][0]["source_url"], "page_number": 2}),
            model="test",
            output_model=EvidenceOutput,
            system_prompt="extract",
            payload=payload,
        )


def test_generic_schema_extractor_uses_strict_json_date_and_decimal_semantics():
    accepted = extract_pydantic(
        _FakeLlm({"event_date": "2026-07-03", "amount": 12.5}),
        model="test", output_model=JsonStrictTypes, system_prompt="extract", payload={},
    )
    assert accepted.event_date == date(2026, 7, 3)
    assert accepted.amount == Decimal("12.5")
    with pytest.raises(ValueError, match="amount"):
        extract_pydantic(
            _FakeLlm({"event_date": "2026-07-03", "amount": "12.5"}),
            model="test", output_model=JsonStrictTypes, system_prompt="extract", payload={},
        )


def test_part06_numeric_string_drops_only_bad_metric_and_preserves_other_metrics():
    period = {
        "report_date": "2025-12-31", "period_type": "annual", "fiscal_year": 2025,
        "revenue": 1000, "parent_net_profit": 100, "currency": "CNY", "unit": "CNY",
        "source_url": "https://example.test/annual",
    }
    def metric(field_id, value, period_name="FY2025"):
        return {
            "field_id": field_id, "value": value, "period": period_name,
            "currency": "CNY", "unit": "CNY", "source_url": "https://example.test/annual",
        }
    output = {
        "annual_inputs": [{
            "financial_period": period,
            "current_assets": metric("current_assets", "1000"),
            "current_liabilities": metric("current_liabilities", 500),
            "inventory": metric("inventory", 100),
        }],
        "latest_balance": {
            "period": "2026Q1",
            "monetary_funds": metric("monetary_funds", 200, "2026Q1"),
            "short_term_borrowings": metric("short_term_borrowings", 300, "2026Q1"),
        },
    }
    result = extract_pydantic(
        _FakeLlm(output), model="test", output_model=Part06Extraction,
        system_prompt="extract", payload={"annual_records": [period]},
    )
    assert result.annual_inputs[0].current_assets is None
    assert result.annual_inputs[0].current_liabilities.value == Decimal("500")
    assert result.annual_inputs[0].inventory.value == Decimal("100")
    assert result.validation_errors == [
        "annual_inputs[0].current_assets.value must be a JSON number, not a numeric string"
    ]


def _part06_latest_balance_output(monetary_funds, short_term_borrowings):
    period = {
        "report_date": "2025-12-31", "period_type": "annual", "fiscal_year": 2025,
        "revenue": 1000, "parent_net_profit": 100, "currency": "CNY", "unit": "CNY",
        "source_url": "https://example.test/annual",
    }
    def metric(field_id, value):
        return {
            "field_id": field_id, "value": value, "period": "2026Q1",
            "currency": "CNY", "unit": "CNY", "source_url": "https://example.test/annual",
        }
    return {
        "annual_inputs": [{"financial_period": period}],
        "latest_balance": {
            "period": "2026Q1",
            "monetary_funds": metric("monetary_funds", monetary_funds),
            "short_term_borrowings": metric("short_term_borrowings", short_term_borrowings),
        },
    }, {"annual_records": [period]}


def test_part06_latest_balance_numeric_string_is_coerced_to_number():
    output, payload = _part06_latest_balance_output("200", 300)
    result = extract_pydantic(
        _FakeLlm(output), model="test", output_model=Part06Extraction,
        system_prompt="extract", payload=payload,
    )
    assert result.latest_balance.monetary_funds.value == Decimal("200")
    assert result.validation_errors == []


def test_part06_latest_balance_non_numeric_string_is_still_rejected():
    output, payload = _part06_latest_balance_output("abc", 300)
    with pytest.raises(ValueError, match="must be a JSON number, not a numeric string"):
        extract_pydantic(
            _FakeLlm(output), model="test", output_model=Part06Extraction,
            system_prompt="extract", payload=payload,
        )


def test_part06_annual_financial_period_revenue_numeric_string_is_coerced():
    period = {
        "report_date": "2025-12-31", "period_type": "annual", "fiscal_year": 2025,
        "revenue": "1000", "parent_net_profit": 100, "currency": "CNY", "unit": "CNY",
        "source_url": "https://example.test/annual",
    }

    def metric(field_id, value):
        return {
            "field_id": field_id, "value": value, "period": "2026Q1",
            "currency": "CNY", "unit": "CNY", "source_url": "https://example.test/annual",
        }

    output = {
        "annual_inputs": [{"financial_period": period}],
        "latest_balance": {
            "period": "2026Q1",
            "monetary_funds": metric("monetary_funds", 200),
            "short_term_borrowings": metric("short_term_borrowings", 300),
        },
    }
    result = extract_pydantic(
        _FakeLlm(output), model="test", output_model=Part06Extraction,
        system_prompt="extract", payload={"annual_records": [period]},
    )
    assert result.annual_inputs[0].financial_period.revenue == Decimal("1000")
    assert result.validation_errors == []


def _part07_balance_sheet_output(accounts_receivable):
    source_url = "https://example.test/annual"
    return {
        "balance_sheet_details": [{
            "fiscal_year": 2025, "report_date": "2025-12-31",
            "accounts_receivable": accounts_receivable, "accounts_receivable_gross": 1200,
            "goodwill": 0, "intangible_assets": 0, "total_assets": 5000,
            "currency": "CNY", "unit": "CNY", "consolidation_scope_id": "group",
            "source_url": source_url, "page_number": 1,
        }],
        "impairment_components": [],
    }, {"documents": [{
        "source_url": source_url, "pages": [{"page_number": 1, "text": "annual balance sheet"}],
    }]}


def test_part07_balance_sheet_numeric_string_is_coerced():
    output, payload = _part07_balance_sheet_output("1000")
    result = extract_pydantic(
        _FakeLlm(output), model="test", output_model=Part07Extraction,
        system_prompt="extract", payload=payload,
    )
    assert result.balance_sheet_details[0].accounts_receivable == Decimal("1000")


def test_part07_balance_sheet_non_numeric_string_is_still_rejected():
    output, payload = _part07_balance_sheet_output("abc")
    with pytest.raises(ValueError, match="must be a JSON number, not a numeric string"):
        extract_pydantic(
            _FakeLlm(output), model="test", output_model=Part07Extraction,
            system_prompt="extract", payload=payload,
        )


def test_part07_receivable_contract_requires_comparable_consecutive_years():
    def item(year, scope="group"):
        return {
            "fiscal_year": year, "report_date": f"{year}-12-31",
            "accounts_receivable": 100, "consolidation_scope_id": scope,
            "source_url": "https://example.test/annual", "page_number": 1,
        }

    accepted = Part07ReceivablesExtraction.model_validate(
        {"receivable_details": [item(2024), item(2025)]}
    )
    assert [value.fiscal_year for value in accepted.receivable_details] == [2025, 2024]
    with pytest.raises(ValueError, match="consecutive"):
        Part07ReceivablesExtraction.model_validate(
            {"receivable_details": [item(2025), item(2023)]}
        )
    with pytest.raises(ValueError, match="same consolidation scope"):
        Part07ReceivablesExtraction.model_validate(
            {"receivable_details": [item(2025), item(2024, "parent")]}
        )


class _RequestsSessionFromHttpxHandler:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url, *, params, timeout):
        request = httpx.Request("GET", url, params=params, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://quote.eastmoney.com/center/gridlist.html",
        })
        return self.handler(request)


def test_shared_announcement_is_exposed_as_cninfo_evidence():
    document = AnnouncementDocument(
        announcement_id="a1",
        stock_code="000938",
        title="董事变更公告",
        published_at=datetime(2026, 1, 1),
        url="https://static.cninfo.com.cn/a1.pdf",
        document_type="announcement",
    )
    bundle = SharedDisclosureBundle(
        org_id="gssz0000938",
        documents=[ParsedDisclosure(
            document=document,
            parsed=ParsedReport(path="/tmp/a1.pdf", pages=[PdfPage(page_number=1, text="董事变更", tables=[])]),
        )],
        announcement_catalog=[document],
    )
    evidence = evidence_text_documents(bundle)[0]
    assert evidence["source"] == "cninfo"
    assert evidence["source_url"] == document.url
    assert "[Page 1]" in evidence["text"]
    assert evidence["pages"] == [{"page_number": 1, "text": "董事变更", "tables": []}]


def test_report_only_evidence_excludes_cninfo_announcements():
    annual = AnnouncementDocument(
        announcement_id="annual",
        stock_code="000938",
        title="2025年年度报告",
        published_at=datetime(2026, 4, 20),
        url="https://static.cninfo.com.cn/annual.pdf",
        document_type="annual_report",
        report_year=2025,
    )
    notice = AnnouncementDocument(
        announcement_id="notice",
        stock_code="000938",
        title="董事变更公告",
        published_at=datetime(2026, 5, 1),
        url="https://static.cninfo.com.cn/notice.pdf",
        document_type="announcement",
    )
    parsed = ParsedReport(
        path="/tmp/report.pdf",
        pages=[PdfPage(page_number=1, text="verified", tables=[])],
    )
    bundle = SharedDisclosureBundle(
        org_id="gssz0000938",
        documents=[
            ParsedDisclosure(document=annual, parsed=parsed),
            ParsedDisclosure(document=notice, parsed=parsed),
        ],
        announcement_catalog=[annual, notice],
    )

    evidence = evidence_text_documents(bundle, reports_only=True)

    assert [item["source"] for item in evidence] == ["annual_report"]


def test_filtered_payload_contains_only_requested_documents_and_pages():
    annual = AnnouncementDocument(
        announcement_id="annual",
        stock_code="000938",
        title="2025年年度报告",
        published_at=datetime(2026, 4, 20),
        url="https://static.cninfo.com.cn/annual.pdf",
        document_type="annual_report",
        report_year=2025,
    )
    notice = AnnouncementDocument(
        announcement_id="notice",
        stock_code="000938",
        title="董事变更公告",
        published_at=datetime(2026, 5, 1),
        url="https://static.cninfo.com.cn/notice.pdf",
        document_type="announcement",
    )
    bundle = SharedDisclosureBundle(
        org_id="org",
        documents=[
            ParsedDisclosure(
                document=annual,
                parsed=ParsedReport(
                    path="/tmp/annual.pdf",
                    pages=[
                        PdfPage(page_number=1, text="unrelated", tables=[]),
                        PdfPage(page_number=2, text="关联交易 details", tables=[]),
                    ],
                ),
            ),
            ParsedDisclosure(
                document=notice,
                parsed=ParsedReport(
                    path="/tmp/notice.pdf",
                    pages=[PdfPage(page_number=1, text="关联交易", tables=[])],
                ),
            ),
        ],
        announcement_catalog=[annual, notice],
    )

    payload = filtered_disclosure_payload(
        bundle,
        document_types={"annual_report"},
        page_pattern=re.compile("关联交易"),
    )

    assert len(payload) == 1
    assert [page["page_number"] for page in payload[0]["pages"]] == [2]


def test_part05_payload_uses_transaction_section_boundaries():
    annual = AnnouncementDocument(
        announcement_id="annual",
        stock_code="000938",
        title="2025年年度报告",
        published_at=datetime(2026, 4, 20),
        url="https://static.cninfo.com.cn/annual.pdf",
        document_type="annual_report",
        report_year=2025,
    )
    texts = [
        "关联方承诺，不属于交易章节",
        "十四、重大关联交易 1、日常经营关联交易",
        "交易表续页",
        "7、其他重大关联交易 十五、重大合同",
        "十二、关联方关系及其交易 5. 关联方交易",
        "5. 关联方交易（续） 关联方租赁",
        "6. 关联方应收应付款项余额",
    ]
    bundle = SharedDisclosureBundle(
        org_id="org",
        documents=[ParsedDisclosure(
            document=annual,
            parsed=ParsedReport(
                path="/tmp/annual.pdf",
                pages=[PdfPage(page_number=index, text=text, tables=[]) for index, text in enumerate(texts, 1)],
            ),
        )],
        announcement_catalog=[annual],
    )

    payloads = part05_related_party_payloads(bundle)

    assert [
        page["page_number"]
        for page in payloads["material_related_party_transactions"][0]["pages"]
    ] == [2, 3, 4]
    assert [
        page["page_number"]
        for page in payloads["financial_note_related_party_transactions"][0]["pages"]
    ] == [5, 6]

    transaction = {
        "related_party": "关联方甲",
        "relationship": "受同一控制人控制",
        "transaction_type": "销售商品",
        "amount_cny": 1000000,
        "period": "FY2025",
        "source": "annual_report",
        "source_url": annual.url,
    }
    llm = _FakeLlm({"related_party_transactions": [transaction]})

    merged = extract_part05_related_party_transactions(
        llm, model="test", bundle=bundle
    )

    assert llm.calls == []
    assert merged == []


def test_news_article_ssl_eof_marks_body_unavailable_without_failing_discovery():
    def handler(request):
        if request.url.host == "www.google.com":
            return httpx.Response(
                200,
                text='<a href="https://media.example.com/story">Story</a>',
                request=request,
            )
        raise ssl.SSLError("unexpected EOF")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        news = discover_negative_news(client, company=_company(), as_of=date(2026, 7, 3))

    assert len(news) == 1
    assert news[0]["body"] is None
    assert "unexpected EOF" in news[0]["body_unavailable_reason"]


def test_shared_disclosures_limit_keyword_announcements_to_rolling_twelve_months(
    monkeypatch, tmp_path
):
    documents = [
        AnnouncementDocument(
            announcement_id="annual-2025",
            stock_code="000938",
            title="2025年年度报告",
            published_at=datetime(2026, 4, 20),
            url="https://static.cninfo.com.cn/annual-2025.pdf",
            document_type="annual_report",
            report_year=2025,
        ),
        AnnouncementDocument(
            announcement_id="annual-2024",
            stock_code="000938",
            title="2024年年度报告",
            published_at=datetime(2025, 4, 20),
            url="https://static.cninfo.com.cn/annual-2024.pdf",
            document_type="annual_report",
            report_year=2024,
        ),
        AnnouncementDocument(
            announcement_id="interim",
            stock_code="000938",
            title="2025年半年度报告",
            published_at=datetime(2025, 8, 20),
            url="https://static.cninfo.com.cn/interim.pdf",
            document_type="interim_report",
            report_year=2025,
        ),
        AnnouncementDocument(
            announcement_id="recent-notice",
            stock_code="000938",
            title="董事变更公告",
            published_at=datetime(2025, 7, 3),
            url="https://static.cninfo.com.cn/recent.pdf",
            document_type="announcement",
        ),
        AnnouncementDocument(
            announcement_id="old-notice",
            stock_code="000938",
            title="董事变更公告",
            published_at=datetime(2025, 7, 2),
            url="https://static.cninfo.com.cn/old.pdf",
            document_type="announcement",
        ),
    ]
    downloaded = []
    monkeypatch.setattr("mlc_agent.production_adapters.discover_cninfo_org_id", lambda *_: "org")
    monkeypatch.setattr("mlc_agent.production_adapters.fetch_company_announcements", lambda *args, **kwargs: documents)
    monkeypatch.setattr("mlc_agent.production_adapters.fetch_szse_announcements", lambda *args, **kwargs: documents)
    monkeypatch.setattr(
        "mlc_agent.production_adapters.download_announcement",
        lambda client, item: downloaded.append(item.announcement_id) or b"pdf",
    )
    monkeypatch.setattr(
        "mlc_agent.production_adapters.parse_machine_generated_pdf",
        lambda path: ParsedReport(path=str(path), pages=[]),
    )

    collect_shared_disclosures(
        object(), company=_company(), as_of=date(2026, 7, 3), run_dir=tmp_path
    )

    assert downloaded == ["annual-2025", "annual-2024", "interim", "recent-notice"]


def test_shared_disclosures_uses_exact_three_year_calendar_boundary(monkeypatch, tmp_path):
    captured = {}

    def fetch(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("mlc_agent.production_adapters.fetch_szse_announcements", fetch)

    collect_shared_disclosures(
        object(), company=_company(), as_of=date(2026, 1, 31), run_dir=tmp_path
    )

    assert captured["start_date"] == date(2023, 1, 31)


def test_fixed_peer_production_adapter_fetches_only_configured_companies(monkeypatch, tmp_path):
    target = CompanyIdentity(
        company_name="Target",
        company_short_name="Target",
        stock_code="000938",
        exchange="深圳证券交易所",
        eastmoney_secid="0.000938",
        eastmoney_secu_code="000938.SZ",
        xueqiu_symbol="SZ000938",
    )
    names = {"000938": "Target", "000034": "神州数码", "000977": "浪潮信息", "603019": "中科曙光"}

    def handler(request):
        report_name = request.url.params["reportName"]
        code = request.url.params["filter"].split('"')[1].split(".")[0]
        if report_name == "RPT_F10_BASIC_ORGINFO":
            data = [{
                "INDUSTRYCSRC1": "C39",
                "EM2016": "计算机设备",
                "ORG_NAME": names[code],
            }]
        else:
            base = 100_000_000 + int(code[-1]) * 1_000_000
            data = [
                {"REPORT_DATE": "2025-12-31 00:00:00", "TOTALOPERATEREVE": base, "PARENTNETPROFIT": base / 10, "GROSSPROFIT": base / 5, "INVENTORYTURNOVER": 3.2, "CURRENCY": "CNY", "UNIT": "CNY"},
                {"REPORT_DATE": "2024-12-31 00:00:00", "TOTALOPERATEREVE": base * 0.9, "PARENTNETPROFIT": base / 11, "GROSSPROFIT": base / 6, "CURRENCY": "CNY", "UNIT": "CNY"},
            ]
        return httpx.Response(200, json={"success": True, "result": {"data": data}}, request=request)

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "fixed_peers.yaml").write_text(
        """version: 1
targets:
  \"000938\":
    company_name: Target
    peers:
      - {company_name: 神州数码, stock_code: \"000034\", exchange: 深圳证券交易所, eastmoney_secid: \"0.000034\", eastmoney_secu_code: 000034.SZ, xueqiu_symbol: SZ000034, selection_rationale: comparable one}
      - {company_name: 浪潮信息, stock_code: \"000977\", exchange: 深圳证券交易所, eastmoney_secid: \"0.000977\", eastmoney_secu_code: 000977.SZ, xueqiu_symbol: SZ000977, selection_rationale: comparable two}
      - {company_name: 中科曙光, stock_code: \"603019\", exchange: 上海证券交易所, eastmoney_secid: \"1.603019\", eastmoney_secu_code: 603019.SH, xueqiu_symbol: SH603019, selection_rationale: comparable three}
""",
        encoding="utf-8",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        target_peer, candidates, identities = collect_fixed_peer_companies(
            client, target=target, config_dir=config_dir
        )
    assert target_peer.csrc_industry == "C39"
    assert target_peer.inventory_turnover.value == Decimal("3.2")
    assert [item.stock_code for item in candidates] == ["000034", "000977", "603019"]
    assert [item.stock_code for item in identities] == ["000034", "000977", "603019"]
