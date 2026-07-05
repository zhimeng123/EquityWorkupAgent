from datetime import date
from decimal import Decimal

from mlc_agent.annual_report_rules import (
    extract_annual_audit,
    extract_annual_litigation,
    extract_annual_regulatory,
    extract_annual_restatement,
)
from mlc_agent.report_parser import ParsedReport, PdfPage


URL = "https://local.invalid/report.pdf"
PUBLISHED = date(2026, 4, 15)


def _report() -> ParsedReport:
    return ParsedReport(path="/tmp/report.pdf", pages=[
        PdfPage(page_number=58, text=(
            "六、与上年度财务报告相比，会计政策、会计估计变更或重大会计差错更正的情况说明\n"
            "□适用 √不适用\n公司报告期无会计政策、会计估计变更或重大会计差错更正的情况。"
        )),
        PdfPage(page_number=60, text=(
            "十一、重大诉讼、仲裁事项\n□适用 √不适用\n"
            "本报告期公司无重大诉讼、仲裁事项。\n其他诉讼事项\n√适用 □不适用"
        )),
        PdfPage(page_number=61, text=(
            "诉讼（仲裁） 涉案金额 是否形成 预计负债\n"
            "公司其他诉讼\n审理、执行阶段\n事项主要为业务合同纠纷 53,068.76 是（注）\n"
            "注：部分诉讼涉及计提预计负债，计提金额对公司本期净利润无重大影响。\n"
            "十二、处罚及整改情况\n□适用 √不适用\n公司报告期不存在处罚及整改情况。"
        )),
        PdfPage(page_number=90, text=(
            "第八节 财务报告\n审计报告\n审计意见类型 标准的无保留意见\n"
            "审计机构名称 安永华明会计师事务所（特殊普通合伙）"
        )),
    ])


def test_deterministic_audit_and_restatement_keep_exact_quotations():
    report = _report()
    audit = extract_annual_audit(
        report, source_url=URL, published_on=PUBLISHED, report_year=2025
    ).latest_audit
    assert audit.auditor_name == "安永华明会计师事务所（特殊普通合伙）"
    assert audit.opinion == "Unmodified"
    assert audit.evidence.evidence_text in report.pages[3].text

    restatement = extract_annual_restatement(
        report, source_url=URL, published_on=PUBLISHED, report_year=2025
    ).restatements
    assert restatement.status == "no"
    assert restatement.negative_evidence.evidence_text in report.pages[0].text


def test_deterministic_litigation_distinguishes_other_litigation_from_no_major():
    report = _report()
    disclosure = extract_annual_litigation(
        report, source_url=URL, published_on=PUBLISHED, report_year=2025
    ).litigation
    assert disclosure.status == "yes"
    assert disclosure.matters[0].amount == Decimal("53068.76")
    assert disclosure.matters[0].evidence.evidence_text in report.pages[2].text


def test_deterministic_regulatory_uses_explicit_negative_statement():
    report = _report()
    regulatory = extract_annual_regulatory(
        report, source_url=URL, published_on=PUBLISHED, report_year=2025
    ).regulatory
    assert regulatory.status == "no"
    assert regulatory.negative_evidence.evidence_text == "公司报告期不存在处罚及整改情况。"
