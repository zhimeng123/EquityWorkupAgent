from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import ssl
from typing import Any, Literal, TypeVar, get_args, get_origin
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup
import httpx
from pydantic import BaseModel, Field, model_validator

from mlc_agent.audit_changes import (
    AuditRecord,
    AuditorChangeEvent,
    MaterialChangeDisclosure,
    RestatementDisclosure,
)
from mlc_agent.litigation import MatterDisclosure
from mlc_agent.llm import chat_completion_text
from mlc_agent.news import NewsArticle
from mlc_agent.company_resolver import build_eastmoney_url
from mlc_agent.cninfo import (
    AnnouncementDocument,
    build_report_catalog,
    download_announcement,
    fetch_company_announcements,
)
from mlc_agent.deep_financial_analysis import (
    AnnualBalanceSheetDetails,
    AnnualReceivableDetails,
    ImpairmentComponent,
)
from mlc_agent.external_links import discover_cninfo_org_id
from mlc_agent.financial_metrics import AnnualLiquidityInput, LatestBalanceSheetInput
from mlc_agent.governance import (
    BoardStructure,
    ControllerChange,
    EmployeeBreakdown,
    GovernanceChange,
    GovernanceReportInput,
    Person,
    ShareholderChangeEvent,
    ShareholderSnapshot,
)
from mlc_agent.eastmoney import fetch_company_profile, fetch_financial_period_records
from mlc_agent.operating_performance import ReportDocument, normalize_financial_periods
from mlc_agent.peer_analysis import DisclosedMetric, PeerCompany
from mlc_agent.related_parties import RelatedPartyTransaction
from mlc_agent.annual_related_parties import extract_annual_related_party_transactions
from mlc_agent.report_parser import ParsedReport, parse_machine_generated_pdf
from mlc_agent.schemas import CompanyIdentity
from mlc_agent.security_analysis import IpoEvidence, SecuritiesOfferingReview
from mlc_agent.evidence_text import anchor_extracted_evidence
from mlc_agent.fixed_peers import load_fixed_peers
from mlc_agent.szse import SZSE_ANNOUNCEMENT_PAGE_URL, fetch_szse_announcements


T = TypeVar("T", bound=BaseModel)


class ParsedDisclosure(BaseModel):
    document: AnnouncementDocument
    parsed: ParsedReport


class SharedDisclosureBundle(BaseModel):
    org_id: str | None = None
    documents: list[ParsedDisclosure]
    announcement_catalog: list[AnnouncementDocument]
    errors: list[str] = Field(default_factory=list)
    catalog_source: Literal["cninfo", "exchange"] = "cninfo"
    catalog_source_url: str | None = None


class Part05Extraction(BaseModel):
    related_party_transactions: list[RelatedPartyTransaction] = Field(default_factory=list)


class Part06Extraction(BaseModel):
    annual_inputs: list[AnnualLiquidityInput]
    latest_balance: LatestBalanceSheetInput
    validation_errors: list[str] = Field(default_factory=list)


class Part07Extraction(BaseModel):
    balance_sheet_details: list[AnnualBalanceSheetDetails]
    impairment_components: list[ImpairmentComponent]


class Part07ReceivablesExtraction(BaseModel):
    receivable_details: list[AnnualReceivableDetails] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_comparable_periods(self) -> "Part07ReceivablesExtraction":
        ordered = sorted(self.receivable_details, key=lambda item: item.fiscal_year, reverse=True)
        if ordered[0].fiscal_year != ordered[1].fiscal_year + 1:
            raise ValueError("receivable details must cover two consecutive fiscal years")
        if ordered[0].consolidation_scope_id != ordered[1].consolidation_scope_id:
            raise ValueError("receivable details must use the same consolidation scope")
        self.receivable_details = ordered
        return self


class Part08Extraction(BaseModel):
    ipo_candidates: list[IpoEvidence] = Field(default_factory=list)
    offering_review: SecuritiesOfferingReview


class Part10CoreExtraction(BaseModel):
    report_period: str
    report_type: Literal["annual_report", "interim_report"]
    source_url: str
    report_date: date
    persons: list[Person]
    board_structure: BoardStructure
    employees: EmployeeBreakdown


class Part10ShareholdersExtraction(BaseModel):
    current_shareholders: ShareholderSnapshot
    previous_shareholders: ShareholderSnapshot | None = None


class Part10ChangesExtraction(BaseModel):
    post_report_changes: list[GovernanceChange] = Field(default_factory=list)
    post_report_shareholder_changes: list[ShareholderChangeEvent] = Field(default_factory=list)
    controller_change: ControllerChange


class Part11AuditExtraction(BaseModel):
    latest_audit: AuditRecord
    previous_audit: AuditRecord | None = None
    post_report_auditor_changes: list[AuditorChangeEvent] = Field(default_factory=list)


class Part11ChangesExtraction(BaseModel):
    restatements: RestatementDisclosure
    board_officer_changes: MaterialChangeDisclosure
    business_operation_changes: MaterialChangeDisclosure
    top3_shareholder_changes: MaterialChangeDisclosure


class Part11RestatementExtraction(BaseModel):
    restatements: RestatementDisclosure


class Part11BoardChangesExtraction(BaseModel):
    board_officer_changes: MaterialChangeDisclosure


class Part11BusinessChangesExtraction(BaseModel):
    business_operation_changes: MaterialChangeDisclosure


class Part11ShareholderChangesExtraction(BaseModel):
    top3_shareholder_changes: MaterialChangeDisclosure


class Part11LegalExtraction(BaseModel):
    litigation: MatterDisclosure
    regulatory: MatterDisclosure


class Part11LitigationExtraction(BaseModel):
    litigation: MatterDisclosure


class Part11RegulatoryExtraction(BaseModel):
    regulatory: MatterDisclosure


class Part11NewsExtraction(BaseModel):
    news_articles: list[NewsArticle] = Field(default_factory=list)


_RELEVANT_ANNOUNCEMENT = re.compile(
    r"收购|并购|重组|董事|监事|高管|股东|审计|会计师|更正|重述|诉讼|仲裁|处罚|监管|发行|配股|增发|可转债|减值"
)
def _column(company: CompanyIdentity) -> str:
    suffix = company.eastmoney_secu_code.rsplit(".", 1)[-1].upper()
    return {"SZ": "szse", "SH": "sse", "BJ": "bjse"}[suffix]


def collect_shared_disclosures(
    client: httpx.Client,
    *,
    company: CompanyIdentity,
    as_of: date,
    run_dir: Path,
) -> SharedDisclosureBundle:
    start_year = as_of.year - 3
    start = date(
        start_year,
        as_of.month,
        min(as_of.day, calendar.monthrange(start_year, as_of.month)[1]),
    )
    suffix = company.eastmoney_secu_code.rsplit(".", 1)[-1].upper()
    if suffix == "SZ":
        org_id = None
        catalog_source = "exchange"
        catalog_source_url = SZSE_ANNOUNCEMENT_PAGE_URL
        catalog_items = fetch_szse_announcements(
            client,
            stock_code=company.stock_code,
            start_date=start,
            end_date=as_of,
            page_size=30,
        )
    else:
        org_id = discover_cninfo_org_id(client, company)
        catalog_source = "cninfo"
        catalog_source_url = None
        catalog_items = fetch_company_announcements(
            client,
            stock_code=company.stock_code,
            org_id=org_id,
            start_date=start,
            end_date=as_of,
            page_size=30,
            column=_column(company),  # type: ignore[arg-type]
        )
    catalog = build_report_catalog(catalog_items)
    announcement_cutoff = date(
        as_of.year - 1,
        as_of.month,
        min(as_of.day, calendar.monthrange(as_of.year - 1, as_of.month)[1]),
    )
    selected: list[AnnouncementDocument] = []
    selected.extend(catalog.annual_reports[:2])
    if catalog.interim_reports:
        selected.append(catalog.interim_reports[0])
    selected.extend(
        item
        for item in catalog.announcements
        if item.document_type == "announcement"
        and item.published_at.date() >= announcement_cutoff
        and _RELEVANT_ANNOUNCEMENT.search(item.title)
    )
    unique = {item.announcement_id: item for item in selected}
    parsed_documents: list[ParsedDisclosure] = []
    errors: list[str] = []
    report_dir = run_dir / "source_documents"
    report_dir.mkdir(parents=True, exist_ok=True)
    for item in unique.values():
        safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", item.announcement_id).strip("_")
        if not safe_id:
            errors.append(f"{item.title}: announcement id cannot form a safe local filename")
            continue
        path = report_dir / f"{safe_id}.pdf"
        try:
            path.write_bytes(download_announcement(client, item))
            parsed_documents.append(
                ParsedDisclosure(document=item, parsed=parse_machine_generated_pdf(path))
            )
        except Exception as exc:
            errors.append(f"{item.title}: {exc}")
    return SharedDisclosureBundle(
        org_id=org_id,
        documents=parsed_documents,
        announcement_catalog=catalog_items,
        errors=errors,
        catalog_source=catalog_source,
        catalog_source_url=catalog_source_url,
    )


def disclosure_payload(bundle: SharedDisclosureBundle) -> list[dict[str, Any]]:
    return filtered_disclosure_payload(bundle)


def part05_related_party_payloads(
    bundle: SharedDisclosureBundle,
) -> dict[str, list[dict[str, Any]]]:
    """Return the latest annual report's two formal transaction blocks separately.

    A keyword match is not sufficient here: references to related parties occur
    throughout an annual report (commitments, balances and other notes).  The
    report itself defines the transaction scope with section boundaries, so keep
    the original pages from those sections and exclude the following section.
    """
    annual_reports = [
        item for item in bundle.documents if item.document.document_type == "annual_report"
    ]
    if not annual_reports:
        return {}
    report = max(annual_reports, key=lambda item: item.document.published_at)
    block_specs = (
        (
            "material_related_party_transactions",
            re.compile(r"十四、\s*重大关联交易"),
            re.compile(r"十五、"),
            True,
        ),
        (
            "financial_note_related_party_transactions",
            re.compile(r"5\.\s*关联方交易"),
            re.compile(r"6\.\s*关联方应收应付款项余额"),
            False,
        ),
    )
    payloads: dict[str, list[dict[str, Any]]] = {}
    document = report.document
    for block_name, start_pattern, end_pattern, include_end_page in block_specs:
        start = next(
            (index for index, page in enumerate(report.parsed.pages) if start_pattern.search(_page_search_text(page))),
            None,
        )
        if start is None:
            continue
        end = next(
            (
                index
                for index, page in enumerate(report.parsed.pages[start + 1 :], start=start + 1)
                if end_pattern.search(_page_search_text(page))
            ),
            len(report.parsed.pages),
        )
        stop = end + 1 if include_end_page and end < len(report.parsed.pages) else end
        payloads[block_name] = [{
            "source": "annual_report",
            "title": document.title,
            "source_url": document.url,
            "published_at": document.published_at.isoformat(),
            "report_year": document.report_year,
            "pages": [
                {
                    "page_number": page.page_number,
                    "text": page.text,
                    "tables": [table.rows for table in page.tables],
                }
                for page in report.parsed.pages[start:stop]
            ],
        }]
    return payloads


def filtered_disclosure_payload(
    bundle: SharedDisclosureBundle,
    *,
    document_types: set[str] | None = None,
    title_pattern: re.Pattern[str] | None = None,
    page_pattern: re.Pattern[str] | None = None,
    adjacent_pages: int = 0,
    latest_per_type: bool = False,
) -> list[dict[str, Any]]:
    documents = [
        item
        for item in bundle.documents
        if (document_types is None or item.document.document_type in document_types)
        and (title_pattern is None or title_pattern.search(item.document.title))
    ]
    if latest_per_type:
        latest: dict[str, ParsedDisclosure] = {}
        for item in documents:
            kind = item.document.document_type
            if kind not in latest or item.document.published_at > latest[kind].document.published_at:
                latest[kind] = item
        documents = list(latest.values())
    def selected_pages(item: ParsedDisclosure) -> list[Any]:
        if page_pattern is None:
            return list(item.parsed.pages)
        matching = {
            index
            for index, page in enumerate(item.parsed.pages)
            if page_pattern.search(_page_search_text(page))
        }
        selected = {
            neighbor
            for index in matching
            for neighbor in range(max(0, index - adjacent_pages), min(len(item.parsed.pages), index + adjacent_pages + 1))
        }
        return [page for index, page in enumerate(item.parsed.pages) if index in selected]

    return [
        {
            "source": (
                item.document.source
                if item.document.document_type == "announcement"
                else item.document.document_type
            ),
            "title": item.document.title,
            "source_url": item.document.url,
            "published_at": item.document.published_at.isoformat(),
            "report_year": item.document.report_year,
            "pages": [
                _page_payload(page)
                for page in selected_pages(item)
            ],
        }
        for item in documents
        if page_pattern is None or any(page_pattern.search(_page_search_text(page)) for page in item.parsed.pages)
    ]


def _page_payload(page: Any) -> dict[str, Any]:
    return {
        "page_number": page.page_number,
        "text": page.text,
        "tables": [table.rows for table in page.tables],
    }


def _page_search_text(page: Any) -> str:
    parts = [page.text]
    for table in page.tables:
        parts.extend("\t".join(str(cell or "") for cell in row) for row in table.rows)
    return "\n".join(part for part in parts if part)


def _page_evidence_text(page: Any) -> str:
    parts = [page.text]
    for table in page.tables:
        parts.extend("\t".join(str(cell or "") for cell in row) for row in table.rows)
    return "\n".join(part for part in parts if part)


def evidence_text_documents(
    bundle: SharedDisclosureBundle,
    *,
    reports_only: bool = False,
    document_types: set[str] | None = None,
    title_pattern: re.Pattern[str] | None = None,
    page_pattern: re.Pattern[str] | None = None,
    adjacent_pages: int = 0,
    latest_per_type: bool = False,
) -> list[dict[str, Any]]:
    documents = [
        item
        for item in bundle.documents
        if (not reports_only or item.document.document_type in {"annual_report", "interim_report"})
        and (document_types is None or item.document.document_type in document_types)
        and (title_pattern is None or title_pattern.search(item.document.title))
    ]
    if latest_per_type:
        latest: dict[str, ParsedDisclosure] = {}
        for item in documents:
            kind = item.document.document_type
            if kind not in latest or item.document.published_at > latest[kind].document.published_at:
                latest[kind] = item
        documents = list(latest.values())
    def selected_pages(item: ParsedDisclosure) -> list[Any]:
        if page_pattern is None:
            return list(item.parsed.pages)
        matching = {
            index
            for index, page in enumerate(item.parsed.pages)
            if page_pattern.search(_page_search_text(page))
        }
        selected = {
            neighbor
            for index in matching
            for neighbor in range(max(0, index - adjacent_pages), min(len(item.parsed.pages), index + adjacent_pages + 1))
        }
        return [page for index, page in enumerate(item.parsed.pages) if index in selected]

    return [
        {
            "source": (
                item.document.source
                if item.document.document_type == "announcement"
                else item.document.document_type
            ),
            "source_url": item.document.url,
            "disclosure_date": item.document.published_at.date().isoformat(),
            "period": (
                f"FY{item.document.report_year}"
                if item.document.report_year is not None
                else item.document.published_at.date().isoformat()
            ),
            "pages": [_page_payload(page) for page in selected_pages(item)],
            "text": "\n".join(
                f"[Page {page.page_number}]\n{_page_evidence_text(page)}"
                for page in selected_pages(item)
            ),
        }
        for item in documents
        if page_pattern is None or any(page_pattern.search(_page_search_text(page)) for page in item.parsed.pages)
    ]


def operating_report_documents(bundle: SharedDisclosureBundle) -> list[ReportDocument]:
    return [
        ReportDocument(
            report=item.parsed,
            source_url=item.document.url,
            report_year=item.document.report_year,
            kind=item.document.document_type,
        )
        for item in bundle.documents
        if item.document.report_year is not None
        and item.document.document_type in {"annual_report", "interim_report"}
    ]


def extract_pydantic(
    llm_client: Any,
    *,
    model: str,
    output_model: type[T],
    system_prompt: str,
    payload: dict[str, Any],
) -> T:
    supplied_pages = [
        f"{document.get('source_url')}: {','.join(str(page.get('page_number')) for page in document.get('pages', []))}"
        for document in payload.get("documents", []) + payload.get("matter_documents", [])
    ]
    allowed_pages = " Supplied source/page pairs (the only allowed pages): " + "; ".join(supplied_pages) if supplied_pages else ""
    content = chat_completion_text(
        llm_client,
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    system_prompt
                    + allowed_pages
                    + " Use only supplied evidence. Every URL, period, date, page, amount and proper name must "
                    "come from that evidence. Do not infer missing facts or create negative answers. "
                    "Every evidence_text must be an exact contiguous substring copied from one supplied page; "
                    "copy it in the source language without translating; never abbreviate it, insert ellipses, "
                    "paraphrase it, or synthesize it from field values. Re-read the selected supplied page and "
                    "verify the quotation character-for-character before returning JSON. "
                    "All monetary values must be normalized to CNY base units only when the disclosed unit is explicit. "
                    "Return only one JSON object matching this JSON Schema exactly: "
                    + json.dumps(output_model.model_json_schema(), ensure_ascii=False)
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        temperature=0,
    )
    if not content:
        raise ValueError(f"LLM returned empty {output_model.__name__} extraction")
    raw = json.loads(content)
    change_models = {
        Part11ChangesExtraction, Part11RestatementExtraction, Part11BoardChangesExtraction,
        Part11BusinessChangesExtraction, Part11ShareholderChangesExtraction,
    }
    legal_models = {Part11LegalExtraction, Part11LitigationExtraction, Part11RegulatoryExtraction}
    if output_model in change_models | legal_models:
        _inject_local_not_disclosed_coverage(raw, payload, legal=output_model in legal_models)
    raw = anchor_extracted_evidence(
        raw, payload.get("documents", []) + payload.get("matter_documents", [])
    )
    if output_model is Part06Extraction:
        validation_errors: list[str] = []
        metric_names = set(AnnualLiquidityInput.model_fields) - {"financial_period"}
        for index, annual in enumerate(raw.get("annual_inputs", [])):
            if not isinstance(annual, dict):
                continue
            for name in metric_names:
                metric = annual.get(name)
                if isinstance(metric, dict) and isinstance(metric.get("value"), str):
                    validation_errors.append(
                        f"annual_inputs[{index}].{name}.value must be a JSON number, not a numeric string"
                    )
                    annual[name] = None
        latest_balance = raw.get("latest_balance")
        if isinstance(latest_balance, dict):
            for name in ("monetary_funds", "short_term_borrowings"):
                metric = latest_balance.get(name)
                if isinstance(metric, dict) and "value" in metric:
                    metric["value"] = _coerce_numeric_string(metric["value"])
        raw["validation_errors"] = validation_errors
    _reject_decimal_strings(raw, output_model)
    # Strict JSON validation intentionally differs from strict Python validation:
    # JSON dates and Decimal numbers are valid, while numeric strings remain invalid.
    result = output_model.model_validate_json(
        json.dumps(raw, ensure_ascii=False), strict=True
    )
    _validate_extracted_evidence(result.model_dump(mode="json"), payload)
    return result


def extract_part05_related_party_transactions(
    llm_client: Any,
    *,
    model: str,
    bundle: SharedDisclosureBundle,
) -> list[RelatedPartyTransaction]:
    """Extract the latest annual report locally; no model or network call is made."""
    annual_reports = [
        item for item in bundle.documents if item.document.document_type == "annual_report"
    ]
    if not annual_reports:
        return []
    latest = max(annual_reports, key=lambda item: item.document.published_at)
    if latest.document.report_year is None:
        raise ValueError("annual report year is required for related-party extraction")
    return extract_annual_related_party_transactions(
        latest.parsed,
        source_url=latest.document.url,
        report_year=latest.document.report_year,
    )


def _inject_local_not_disclosed_coverage(
    raw: Any,
    payload: dict[str, Any],
    *,
    legal: bool,
) -> None:
    """Use an actually supplied topic page as coverage; never use an arbitrary page."""
    documents = payload.get("documents", []) + payload.get("matter_documents", [])
    topic_pattern = (
        re.compile(r"诉讼|仲裁|处罚|监管") if legal else
        re.compile(r"更正|重述|董事|高级管理人员|经营|业务|股东")
    )
    supplied = next(
        (
            (document, page)
            for document in documents
            for page in document.get("pages", [])
            if topic_pattern.search(str(page.get("text") or ""))
        ),
        None,
    )
    if supplied is None:
        return
    document, page = supplied
    text = str(page["text"]).strip()
    excerpt = text[:300]
    source = document.get("source") or "cninfo"
    disclosure_date = str(document.get("published_at") or "")[:10]
    evidence: dict[str, Any] = {
        "source": source,
        "source_url": document["source_url"],
        "disclosure_date": disclosure_date,
        "evidence_text": excerpt,
        "page_number": page["page_number"],
    }
    if not legal:
        report_year = document.get("report_year")
        evidence["period"] = f"FY{report_year}" if report_year else disclosure_date

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("status") == "not_disclosed":
                value["coverage_evidence"] = dict(evidence)
                value["negative_evidence"] = None
                if "events" in value:
                    value["events"] = []
                if "matters" in value:
                    value["matters"] = []
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(raw)


def _coerce_numeric_string(value: Any) -> Any:
    """Coerce a JSON numeric string to its int/float value; leave any other value unchanged."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
            return parsed
    return value


def _reject_decimal_strings(value: Any, annotation: Any, path: str = "") -> None:
    """Pydantic accepts JSON strings for Decimal even in strict JSON mode; our contract does not."""
    if annotation is Decimal:
        if isinstance(value, str):
            raise ValueError(f"{path or 'value'} must be a JSON number, not a numeric string")
        return
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (list, tuple, set, frozenset) and isinstance(value, list) and args:
        for index, item in enumerate(value):
            _reject_decimal_strings(item, args[0], f"{path}[{index}]")
        return
    if origin is not None and args:
        for candidate in args:
            if candidate is type(None):
                continue
            _reject_decimal_strings(value, candidate, path)
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel) and isinstance(value, dict):
        for name, field in annotation.model_fields.items():
            if name in value:
                _reject_decimal_strings(value[name], field.annotation, f"{path}.{name}".lstrip("."))


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _validate_extracted_evidence(output: Any, payload: dict[str, Any]) -> None:
    allowed_urls = {
        str(value)
        for item in _walk(payload)
        if isinstance(item, dict)
        for key, value in item.items()
        if key in {"source_url", "original_url", "catalog_source_url"} and value
    }
    page_limits = {}
    for document in payload.get("documents", []) + payload.get("matter_documents", []):
        if not isinstance(document, dict) or not document.get("source_url"):
            continue
        pages = document.get("pages", [])
        if pages:
            page_limits[str(document["source_url"])] = {
                int(page["page_number"]) for page in pages
            }
    for item in _walk(output):
        if not isinstance(item, dict):
            continue
        for key in ("source_url", "original_url", "catalog_source_url"):
            url = item.get(key)
            if url and str(url) not in allowed_urls:
                raise ValueError(f"extracted evidence URL was not supplied: {url}")
        source_url = str(item.get("source_url") or "")
        page = item.get("page_number", item.get("source_page"))
        if page is not None and source_url in page_limits:
            if int(page) not in page_limits[source_url]:
                raise ValueError(
                    f"extracted evidence page {page} is outside supplied document {source_url}"
                )


def discover_negative_news(
    client: httpx.Client,
    *,
    company: CompanyIdentity,
    as_of: date,
) -> list[dict[str, Any]]:
    queries = [
        f'"{company.company_name}" 处罚 OR 诉讼 OR 调查 OR 负面',
        f'"{company.company_english_name or company.company_name}" penalty OR lawsuit OR investigation',
    ]
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query, language in zip(queries, ("zh", "en"), strict=True):
        response = client.get("https://www.google.com/search", params={"q": query})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "")
            if href.startswith("/url?"):
                href = parse_qs(urlparse(href).query).get("q", [""])[0]
            href = unquote(href)
            host = (urlparse(href).hostname or "").lower()
            if not href.startswith("http") or "google." in host or href in seen:
                continue
            seen.add(href)
            title = " ".join(anchor.get_text(" ", strip=True).split())
            try:
                article_response = client.get(href)
                article_response.raise_for_status()
                article_soup = BeautifulSoup(article_response.text, "html.parser")
                body = " ".join(article_soup.get_text(" ", strip=True).split())[:30000]
            except (httpx.HTTPError, ssl.SSLError) as exc:
                body = None
                body_unavailable_reason = str(exc)
            else:
                body_unavailable_reason = None
            output.append(
                {
                    "discovery_language": language,
                    "title": title or href,
                    "original_url": href,
                    "body": body,
                    "body_unavailable_reason": body_unavailable_reason,
                    "as_of": as_of.isoformat(),
                }
            )
    return output


def _inventory_turnover(record: dict[str, Any]) -> Any:
    for key in ("INVENTORYTURNOVER", "INVENTORY_TURNOVER", "CHZZL"):
        if record.get(key) not in (None, ""):
            return record[key]
    return None


def collect_fixed_peer_companies(
    client: httpx.Client,
    *,
    target: CompanyIdentity,
    config_dir: Path,
) -> tuple[PeerCompany, list[PeerCompany], list[CompanyIdentity]]:
    fixed_peer_set = load_fixed_peers(config_dir, target.stock_code)
    identities = [peer.identity() for peer in fixed_peer_set.peers]
    try:
        target_profile = fetch_company_profile(client, target)
    except Exception as exc:
        raise RuntimeError(
            f"peer target profile GET {build_eastmoney_url(target)} failed: {exc}"
        ) from exc
    target_industry = str(target_profile.get("INDUSTRYCSRC1") or "").strip()
    if not target_industry:
        raise ValueError("Eastmoney profile lacks CSRC industry classification")

    def build(company: CompanyIdentity, industry: str) -> PeerCompany:
        raw = fetch_financial_period_records(client, company)
        records = normalize_financial_periods(
            raw,
            source_url=build_eastmoney_url(company),
        )
        annual = next((item for item in records if item.period_type == "annual"), None)
        raw_annual = next(
            (
                item
                for item in raw
                if str(item.get("REPORT_DATE") or "").startswith(
                    annual.report_date.isoformat() if annual else "missing"
                )
            ),
            None,
        )
        turnover = _inventory_turnover(raw_annual or {})
        return PeerCompany(
            stock_code=company.stock_code,
            company_name=company.company_name,
            csrc_industry=industry,
            is_a_share=True,
            is_st=company.company_short_name.upper().startswith(("ST", "*ST")),
            is_financial=industry.startswith("J") or "金融" in industry,
            annual_records=records,
            inventory_turnover=(
                DisclosedMetric(
                    value=turnover,
                    period=f"FY{annual.fiscal_year}",
                    source_url=build_eastmoney_url(company),
                )
                if turnover is not None and annual is not None
                else None
            ),
        )

    try:
        target_peer = build(target, target_industry)
    except Exception as exc:
        raise RuntimeError(
            f"peer target financials GET {build_eastmoney_url(target)} failed: {exc}"
        ) from exc
    candidates: list[PeerCompany] = []
    for identity in identities:
        try:
            candidates.append(build(identity, target_industry))
        except Exception as exc:
            raise RuntimeError(
                f"peer candidate financials GET {build_eastmoney_url(identity)} failed: {exc}"
            ) from exc
    return target_peer, candidates, identities
