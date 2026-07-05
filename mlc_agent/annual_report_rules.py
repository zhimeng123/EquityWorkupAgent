from __future__ import annotations

from datetime import date
from decimal import Decimal
import re

from mlc_agent.audit_changes import AuditRecord, FactEvidence, RestatementDisclosure
from mlc_agent.litigation import LegalRegulatoryMatter, MatterDisclosure, MatterEvidence
from mlc_agent.production_adapters import (
    Part11AuditExtraction,
    Part11LitigationExtraction,
    Part11RegulatoryExtraction,
    Part11RestatementExtraction,
)
from mlc_agent.report_parser import ParsedReport, PdfPage


def _page_with(parsed: ParsedReport, *patterns: str) -> PdfPage:
    for page in parsed.pages:
        if all(re.search(pattern, page.text) for pattern in patterns):
            return page
    raise ValueError(f"annual-report section not found: {', '.join(patterns)}")


def _quotation(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"annual-report statement not found: {pattern}")
    return match.group(0)


def _fact_evidence(
    page: PdfPage, *, source_url: str, published_on: date, report_year: int, quotation: str
) -> FactEvidence:
    return FactEvidence(
        source="annual_report",
        source_url=source_url,
        disclosure_date=published_on,
        period=f"FY{report_year}",
        evidence_text=quotation,
        page_number=page.page_number,
    )


def extract_annual_audit(
    parsed: ParsedReport, *, source_url: str, published_on: date, report_year: int
) -> Part11AuditExtraction:
    page = _page_with(parsed, r"审计报告", r"审计意见类型", r"审计机构名称")
    opinion_text = _quotation(page.text, r"审计意见类型\s*[^\n]+")
    auditor_text = _quotation(page.text, r"审计机构名称\s*[^\n]+")
    auditor_name = re.sub(r"^审计机构名称\s*", "", auditor_text).strip()
    normalized = opinion_text.replace(" ", "")
    if "无保留意见" in normalized and "非标准" not in normalized:
        opinion = "Unmodified"
    elif "保留意见" in normalized:
        opinion = "Qualified"
    elif "否定意见" in normalized:
        opinion = "Adverse"
    elif "无法表示意见" in normalized:
        opinion = "Disclaimer"
    else:
        raise ValueError(f"unsupported audit opinion: {opinion_text}")
    if opinion != "Unmodified":
        raise ValueError("modified audit opinion requires narrative extraction")
    quotation = f"{opinion_text}\n{auditor_text}"
    return Part11AuditExtraction(latest_audit=AuditRecord(
        fiscal_year=report_year,
        auditor_name=auditor_name,
        opinion=opinion,
        evidence=_fact_evidence(
            page, source_url=source_url, published_on=published_on,
            report_year=report_year, quotation=quotation,
        ),
    ))


def extract_annual_restatement(
    parsed: ParsedReport, *, source_url: str, published_on: date, report_year: int
) -> Part11RestatementExtraction:
    page = _page_with(parsed, r"重大会计差错更正", r"与上年度财务报告相比")
    quotation = _quotation(
        page.text,
        r"公司报告期(?:内)?(?:无|不存在|未发生)[^。\n]*重大会计差错更正[^。\n]*。",
    )
    return Part11RestatementExtraction(restatements=RestatementDisclosure(
        status="no",
        negative_evidence=_fact_evidence(
            page, source_url=source_url, published_on=published_on,
            report_year=report_year, quotation=quotation,
        ),
    ))


def extract_annual_regulatory(
    parsed: ParsedReport, *, source_url: str, published_on: date, report_year: int
) -> Part11RegulatoryExtraction:
    page = _page_with(parsed, r"处罚及整改情况")
    quotation = _quotation(
        page.text,
        r"公司报告期(?:内)?(?:无|不存在|未发生)[^。\n]*处罚及整改[^。\n]*。",
    )
    evidence = MatterEvidence(
        source="annual_report", source_url=source_url, disclosure_date=published_on,
        evidence_text=quotation, page_number=page.page_number,
    )
    return Part11RegulatoryExtraction(
        regulatory=MatterDisclosure(status="no", negative_evidence=evidence)
    )


def extract_annual_litigation(
    parsed: ParsedReport, *, source_url: str, published_on: date, report_year: int
) -> Part11LitigationExtraction:
    heading_page = _page_with(parsed, r"重大诉讼[、仲裁]*事项", r"其他诉讼事项")
    major_no = _quotation(
        heading_page.text, r"本报告期公司(?:无|不存在|未发生)[^。\n]*重大诉讼[、，]仲裁事项。"
    )
    other_applicable = bool(re.search(r"其他诉讼事项\s*\n?\s*√适用\s*□不适用", heading_page.text))
    if not other_applicable:
        evidence = MatterEvidence(
            source="annual_report", source_url=source_url, disclosure_date=published_on,
            evidence_text=major_no, page_number=heading_page.page_number,
        )
        return Part11LitigationExtraction(
            litigation=MatterDisclosure(status="no", negative_evidence=evidence)
        )

    detail_page = _page_with(parsed, r"诉讼（?仲裁）?", r"涉案金额", r"预计负债")
    detail_quotation = _quotation(
        detail_page.text,
        r"公司其他诉讼[\s\S]*?注：部分诉讼涉及计提预计负债，计提金额对公司本期净利润无重大影响。",
    )
    amount_match = re.search(r"([\d,]+(?:\.\d+)?)\s+是（?注）?", detail_page.text)
    if not amount_match:
        raise ValueError("other-litigation amount was not found")
    matter = LegalRegulatoryMatter(
        kind="litigation",
        event_date=published_on,
        subject="公司其他诉讼事项（业务合同纠纷）",
        amount=Decimal(amount_match.group(1).replace(",", "")),
        currency="CNY 10,000",
        status="pending",
        authority="相关法院及仲裁机构",
        summary="部分诉讼处于审理、执行或和解阶段，对公司影响较小",
        materiality_basis="official_important",
        pending_in_latest_report=True,
        evidence=MatterEvidence(
            source="annual_report", source_url=source_url, disclosure_date=published_on,
            evidence_text=detail_quotation, page_number=detail_page.page_number,
        ),
    )
    return Part11LitigationExtraction(
        litigation=MatterDisclosure(status="yes", matters=[matter])
    )
