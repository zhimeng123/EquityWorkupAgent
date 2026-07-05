from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from mlc_agent.related_parties import RelatedPartyTransaction
from mlc_agent.report_parser import ParsedReport


_AMOUNT = r"(?:-|\(?[\d,]+(?:\.\d+)?\)?)"
_NOTE_ROW = re.compile(
    rf"^(?P<party>.+?)\s+(?P<kind>产品采购|技术服务|物业配套服务|代建服务|"
    rf"产品销售(?:/技术服务)?(?:/物业配套服务)?|房屋及建筑物|设备)\s+"
    rf"(?P<current>{_AMOUNT})(?:\s+{_AMOUNT})+(?:\s+否)?$"
)


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").strip()


def _decimal(value: str, multiplier: Decimal) -> Decimal | None:
    raw = value.strip().replace(",", "")
    if raw == "-":
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1]
    try:
        parsed = Decimal(raw) * multiplier
    except InvalidOperation:
        return None
    return -parsed if negative else parsed


def _section_pages(
    report: ParsedReport, start: re.Pattern[str], end: re.Pattern[str]
) -> list:
    start_index = next(
        (index for index, page in enumerate(report.pages) if start.search(page.text)), None
    )
    if start_index is None:
        return []
    end_index = next(
        (
            index
            for index, page in enumerate(report.pages[start_index + 1 :], start_index + 1)
            if end.search(page.text)
        ),
        len(report.pages),
    )
    return report.pages[start_index:end_index]


def _material_transactions(
    report: ParsedReport, *, source_url: str, period: str
) -> list[RelatedPartyTransaction]:
    pages = _section_pages(
        report, re.compile(r"十四、\s*重大关联交易"), re.compile(r"十五、")
    )
    result: list[RelatedPartyTransaction] = []
    for page in pages:
        for table in page.tables:
            for row in table.rows:
                # The formal daily-transaction table has 14 columns.  Rows from
                # narrative summary tables and totals are deliberately rejected.
                if len(row) != 14:
                    continue
                party, relationship, kind, terms = (
                    _clean(row[0]), _clean(row[1]), _clean(row[3]), _clean(row[10])
                )
                amount = _decimal(_clean(row[6]), Decimal("10000"))
                if (
                    not party
                    or party == "合计"
                    or not relationship
                    or not kind
                    or amount is None
                    or amount < 0
                ):
                    continue
                result.append(RelatedPartyTransaction(
                    related_party=party,
                    relationship=relationship,
                    transaction_type=kind,
                    amount_cny=amount,
                    commercial_terms=terms or "Not disclosed",
                    board_approved="Not disclosed",
                    period=period,
                    source="annual_report",
                    source_url=source_url,
                    source_page=page.page_number,
                ))
    return result


def _note_mode(text: str) -> str | None:
    if "自关联方购买商品和接受劳务" in text:
        return "purchase"
    if "向关联方销售商品和提供劳务" in text:
        return "sale"
    if "关联方租赁" in text:
        return "lease"
    return None


def _note_transactions(
    report: ParsedReport, *, source_url: str, period: str
) -> list[RelatedPartyTransaction]:
    pages = _section_pages(
        report,
        re.compile(r"5\.\s*关联方交易(?:（续）)?"),
        re.compile(r"6\.\s*关联方应收应付款项余额"),
    )
    result: list[RelatedPartyTransaction] = []
    for page in pages:
        mode = _note_mode(page.text)
        if mode is None:
            continue
        pending = ""
        lease_lessor = mode == "lease"
        for raw_line in page.text.splitlines():
            line = raw_line.strip()
            if line == "作为承租人":
                lease_lessor = False
                pending = ""
                continue
            if mode == "lease" and not lease_lessor:
                continue
            if (
                not line
                or line.startswith(("紫光股份有限公司", "财务报表附注", "十二、", "5.", "（"))
                or re.search(r"交易内容|是否超过|租赁资产种类|作为出租人|^202[45]年", line)
            ):
                pending = ""
                continue
            candidate = f"{pending}{line}" if pending else line
            match = _NOTE_ROW.match(candidate)
            if match is None:
                # A wrapped company or transaction-kind cell has no amount on
                # the first physical line.  Only retain it for the immediately
                # following line; headings are never promoted into data rows.
                pending = (
                    candidate
                    if not re.search(r"\d[\d,.]*(?:\)|$)", candidate)
                    and (
                        candidate.endswith("有限")
                        or bool(re.match(r"^[A-Za-z].*(?:Ltd\.|Co\.)?$", candidate))
                    )
                    else ""
                )
                continue
            pending = ""
            party = _clean(match.group("party"))
            kind = _clean(match.group("kind"))
            amount = _decimal(match.group("current"), Decimal("1"))
            if not party or amount is None or amount < 0:
                continue
            transaction_type = {
                "purchase": kind,
                "sale": kind,
                "lease": "租赁收入" if "作为出租人" in page.text else "租赁支出",
            }[mode]
            result.append(RelatedPartyTransaction(
                related_party=party,
                relationship="关联方",
                transaction_type=transaction_type,
                amount_cny=amount,
                commercial_terms="Not disclosed",
                board_approved="Not disclosed",
                period=period,
                source="annual_report",
                source_url=source_url,
                source_page=page.page_number,
            ))
    return result


def _semantic_type(value: str) -> str:
    value = _clean(value)
    if "采购" in value:
        return "purchase"
    if "销售" in value:
        return "sale"
    if "服务" in value:
        return "service"
    if "租赁" in value or "房屋" in value or "物业" in value:
        return "lease"
    return value


def extract_annual_related_party_transactions(
    report: ParsedReport,
    *,
    source_url: str,
    report_year: int,
) -> list[RelatedPartyTransaction]:
    """Extract complete transaction rows from the two formal annual-report sections.

    Financial-statement notes use yuan and are more precise than the rounded
    `万元` management-report table.  A note row therefore replaces the rounded
    row for the same party and semantic transaction type.  No missing cell is
    inferred and incomplete rows are omitted.
    """
    period = f"FY{report_year}"
    material = _material_transactions(report, source_url=source_url, period=period)
    notes = _note_transactions(report, source_url=source_url, period=period)
    note_keys = {
        (_clean(item.related_party), _semantic_type(item.transaction_type)) for item in notes
    }
    combined = [
        item for item in material
        if (_clean(item.related_party), _semantic_type(item.transaction_type)) not in note_keys
    ] + notes
    unique: dict[tuple[str, str, Decimal], RelatedPartyTransaction] = {}
    for item in combined:
        assert item.amount_cny is not None
        key = (_clean(item.related_party), _semantic_type(item.transaction_type), item.amount_cny)
        unique.setdefault(key, item)
    return list(unique.values())
