from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, model_validator

from mlc_agent.company_resolver import build_eastmoney_url
from mlc_agent.report_parser import ParsedReport
from mlc_agent.schemas import CompanyIdentity, ItemEvidence, SourceValue, WorkupAgentState


PeriodType = Literal["q1_ytd", "half_year_ytd", "q3_ytd", "annual"]
ReportKind = Literal["annual_report", "interim_report"]


class FinancialPeriodRecord(BaseModel):
    report_date: date
    period_type: PeriodType
    fiscal_year: int
    currency: str = "CNY"
    unit: str = "CNY"
    revenue: Decimal | None = None
    parent_net_profit: Decimal | None = None
    gross_profit: Decimal | None = None
    source_url: str
    raw_record: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_date_matches_period(self) -> "FinancialPeriodRecord":
        expected = {
            "q1_ytd": (3, 31),
            "half_year_ytd": (6, 30),
            "q3_ytd": (9, 30),
            "annual": (12, 31),
        }[self.period_type]
        if (self.report_date.month, self.report_date.day) != expected:
            raise ValueError(
                f"{self.period_type} must end on {expected[0]:02d}-{expected[1]:02d}"
            )
        if self.report_date.year != self.fiscal_year:
            raise ValueError("report_date year must equal fiscal_year")
        return self


class StandaloneQuarterRecord(BaseModel):
    report_date: date
    fiscal_year: int
    fiscal_quarter: int = Field(ge=1, le=4)
    currency: str
    unit: str
    revenue: Decimal | None = None
    parent_net_profit: Decimal | None = None
    gross_profit: Decimal | None = None
    gross_margin: Decimal | None = None
    source_url: str
    formulas: dict[str, str] = Field(default_factory=dict)


class ReportDocument(BaseModel):
    report: ParsedReport
    source_url: str
    report_year: int
    kind: ReportKind
    currency: str = "CNY"


class BreakdownItem(BaseModel):
    category: Literal["business_segment", "geography"]
    name: str
    revenue_cny_million: Decimal
    report_year: int
    report_kind: ReportKind
    source_url: str
    page_number: int
    original_value: str
    original_unit: str
    conversion_formula: str


class ConcentrationItem(BaseModel):
    kind: Literal["customer", "supplier"]
    disclosed_name: str
    amount_cny_million: Decimal | None = None
    concentration_percent: Decimal | None = None
    source_url: str
    page_number: int


class OutlookRiskItem(BaseModel):
    kind: Literal["outlook", "risk"]
    text: str
    source_url: str
    page_number: int


class OperatingPerformanceResult(BaseModel):
    source_values: list[SourceValue]
    annual_records: list[FinancialPeriodRecord]
    ytd_records: list[FinancialPeriodRecord]
    quarterly_records: list[StandaloneQuarterRecord]
    breakdown_items: list[BreakdownItem]
    concentration_items: list[ConcentrationItem]
    outlook_risk_items: list[OutlookRiskItem]
    errors: list[str] = Field(default_factory=list)


OutlookEnglishRenderer = Callable[[tuple[OutlookRiskItem, ...]], str]


def build_outlook_english_prompt(items: tuple[OutlookRiskItem, ...]) -> str:
    """Build the strict prompt used by an injected English renderer adapter."""
    verified = [
        {"kind": item.kind, "text": item.text}
        for item in items
    ]
    return (
        "Render only the verified disclosures below as concise English bullet points. "
        "Do not add, infer, combine, omit, or soften any fact. Preserve Chinese proper "
        "names exactly when no official English name is supplied; all surrounding "
        "narrative must be English. Return only the rendered bullet points.\n"
        + json.dumps(verified, ensure_ascii=False)
    )


_PERIOD_BY_MONTH_DAY: dict[tuple[int, int], PeriodType] = {
    (3, 31): "q1_ytd",
    (6, 30): "half_year_ytd",
    (9, 30): "q3_ytd",
    (12, 31): "annual",
}

_FINANCIAL_UNIT_TO_CNY = {
    "CNY": Decimal("1"),
    "元": Decimal("1"),
    "千元": Decimal("1000"),
    "万元": Decimal("10000"),
    "百万元": Decimal("1000000"),
    "亿元": Decimal("100000000"),
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric value: {value!r}") from exc


def _record_value(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if record.get(name) not in (None, ""):
            return record[name]
    return None


def normalize_financial_periods(
    raw_records: list[dict[str, Any]],
    *,
    source_url: str,
) -> list[FinancialPeriodRecord]:
    """Normalize cumulative Eastmoney records; unsupported dates are rejected."""
    normalized: list[FinancialPeriodRecord] = []
    seen: set[tuple[date, str]] = set()
    for raw in raw_records:
        raw_date = str(raw.get("REPORT_DATE") or "").split(" ", 1)[0]
        try:
            report_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError(f"invalid or missing REPORT_DATE: {raw_date!r}") from exc
        period_type = _PERIOD_BY_MONTH_DAY.get((report_date.month, report_date.day))
        if period_type is None:
            raise ValueError(f"unsupported financial period end: {report_date.isoformat()}")
        currency = str(raw.get("CURRENCY") or "CNY").upper()
        original_unit = str(raw.get("UNIT") or "CNY")
        if currency != "CNY":
            raise ValueError(f"unsupported financial record currency: {currency}")
        if original_unit not in _FINANCIAL_UNIT_TO_CNY:
            raise ValueError(f"unsupported financial record unit: {original_unit}")
        key = (report_date, period_type)
        if key in seen:
            raise ValueError(f"duplicate financial period: {report_date.isoformat()}")
        seen.add(key)
        unit_factor = _FINANCIAL_UNIT_TO_CNY[original_unit]
        revenue = _decimal(_record_value(raw, "TOTALOPERATEREVE", "TOTAL_OPERATE_REVENUE"))
        parent_profit = _decimal(_record_value(raw, "PARENTNETPROFIT", "PARENT_NET_PROFIT"))
        gross_profit = _decimal(_record_value(raw, "GROSSPROFIT", "GROSS_PROFIT"))
        revenue = revenue * unit_factor if revenue is not None else None
        parent_profit = parent_profit * unit_factor if parent_profit is not None else None
        gross_profit = gross_profit * unit_factor if gross_profit is not None else None
        if gross_profit is None and revenue is not None:
            margin = _decimal(_record_value(raw, "XSMLL", "GROSS_MARGIN"))
            if margin is not None:
                gross_profit = revenue * margin / Decimal("100")
        normalized.append(
            FinancialPeriodRecord(
                report_date=report_date,
                period_type=period_type,
                fiscal_year=report_date.year,
                currency=currency,
                unit="CNY",
                revenue=revenue,
                parent_net_profit=parent_profit,
                gross_profit=gross_profit,
                source_url=source_url,
                raw_record=raw,
            )
        )
    return sorted(normalized, key=lambda item: item.report_date, reverse=True)


def select_operating_periods(
    records: list[FinancialPeriodRecord],
) -> tuple[list[FinancialPeriodRecord], FinancialPeriodRecord | None]:
    annual = sorted(
        (item for item in records if item.period_type == "annual"),
        key=lambda item: item.report_date,
        reverse=True,
    )[:2]
    interim = next(
        (
            item
            for item in sorted(records, key=lambda value: value.report_date, reverse=True)
            if item.period_type == "half_year_ytd"
            and (not annual or item.report_date > annual[0].report_date)
        ),
        None,
    )
    return annual, interim


def restore_standalone_quarters(
    records: list[FinancialPeriodRecord],
) -> tuple[list[StandaloneQuarterRecord], list[str]]:
    """Restore only the latest eight standard quarters with required adjacent YTD data."""
    quarter_by_period: dict[PeriodType, int] = {
        "q1_ytd": 1,
        "half_year_ytd": 2,
        "q3_ytd": 3,
        "annual": 4,
    }
    if not records:
        return [], []
    latest_ordinal = max(
        item.fiscal_year * 4 + quarter_by_period[item.period_type]
        for item in records
    )
    earliest_ordinal = latest_ordinal - 7
    by_year = {
        year: {item.period_type: item for item in records if item.fiscal_year == year}
        for year in {item.fiscal_year for item in records}
    }
    output: list[StandaloneQuarterRecord] = []
    errors: list[str] = []
    quarter_specs = [
        (1, "q1_ytd", None),
        (2, "half_year_ytd", "q1_ytd"),
        (3, "q3_ytd", "half_year_ytd"),
        (4, "annual", "q3_ytd"),
    ]
    for year in sorted(by_year, reverse=True):
        periods = by_year[year]
        for quarter, current_name, previous_name in quarter_specs:
            ordinal = year * 4 + quarter
            if ordinal < earliest_ordinal or ordinal > latest_ordinal:
                continue
            current = periods.get(current_name)  # type: ignore[arg-type]
            if current is None:
                continue
            previous = periods.get(previous_name) if previous_name else None  # type: ignore[arg-type]
            if previous_name and previous is None:
                errors.append(f"FY{year} Q{quarter}: missing adjacent {previous_name}")
                continue
            values: dict[str, Decimal | None] = {}
            formulas: dict[str, str] = {}
            for metric in ("revenue", "parent_net_profit", "gross_profit"):
                current_value = getattr(current, metric)
                previous_value = getattr(previous, metric) if previous else None
                if current_value is None or (previous_name and previous_value is None):
                    values[metric] = None
                    continue
                values[metric] = current_value if previous is None else current_value - previous_value
                formulas[metric] = (
                    f"{current_name}.{metric}"
                    if previous is None
                    else f"{current_name}.{metric} - {previous_name}.{metric}"
                )
            gross_margin = None
            if values["gross_profit"] is not None and values["revenue"] not in (None, Decimal("0")):
                gross_margin = values["gross_profit"] / values["revenue"] * Decimal("100")
                formulas["gross_margin"] = "standalone gross_profit / standalone revenue * 100"
            output.append(
                StandaloneQuarterRecord(
                    report_date=current.report_date,
                    fiscal_year=year,
                    fiscal_quarter=quarter,
                    currency=current.currency,
                    unit=current.unit,
                    revenue=values["revenue"],
                    parent_net_profit=values["parent_net_profit"],
                    gross_profit=values["gross_profit"],
                    gross_margin=gross_margin,
                    source_url=current.source_url,
                    formulas=formulas,
                )
            )
    return sorted(output, key=lambda item: (item.fiscal_year, item.fiscal_quarter), reverse=True), errors


_UNIT_FACTORS = {
    "元": Decimal("0.000001"),
    "千元": Decimal("0.001"),
    "万元": Decimal("0.01"),
    "百万元": Decimal("1"),
    "亿元": Decimal("100"),
}


def convert_to_cny_million(value: Any, original_unit: str) -> tuple[Decimal, str]:
    if original_unit not in _UNIT_FACTORS:
        raise ValueError(f"unsupported CNY unit: {original_unit}")
    number = _decimal(value)
    if number is None:
        raise ValueError("amount is required")
    factor = _UNIT_FACTORS[original_unit]
    return number * factor, f"{number} {original_unit} * {factor} = {number * factor} CNY million"


def _page_unit(text: str) -> str | None:
    match = re.search(r"单位[：:]\s*(?:人民币)?\s*(百万元|亿元|万元|千元|元)", text)
    return match.group(1) if match else None


def extract_revenue_breakdown(documents: list[ReportDocument]) -> list[BreakdownItem]:
    items: list[BreakdownItem] = []
    category_markers = {
        "business_segment": ("分行业", "分产品", "业务分部"),
        "geography": ("分地区",),
    }
    for document in documents:
        for page in document.report.pages:
            unit = _page_unit(page.text)
            for table in page.tables:
                rows = table.rows
                if len(rows) < 2:
                    continue
                if unit is None:
                    continue

                # A-share annual reports commonly use a multi-row composition table:
                # row 0 carries year labels, row 2 carries "金额", and marker rows
                # such as "分产品" / "分地区" delimit the data blocks.  Parse that
                # disclosed structure directly; a single-row-header assumption drops
                # the entire table.
                year_columns = {
                    index: int(match.group(1))
                    for index, value in enumerate(rows[0])
                    if value and (match := re.fullmatch(r"\s*(20\d{2})年\s*", value))
                }
                amount_row = next(
                    (
                        row
                        for row in rows[1:4]
                        if any("金额" in (value or "") for value in row)
                    ),
                    None,
                )
                if year_columns and amount_row is not None:
                    amount_columns = {
                        index: year
                        for index, year in year_columns.items()
                        if index < len(amount_row) and "金额" in (amount_row[index] or "")
                    }
                    category: Literal["business_segment", "geography"] | None = None
                    for row in rows:
                        label = (row[0] or "").strip() if row else ""
                        marker_category = next(
                            (
                                name
                                for name, markers in category_markers.items()
                                if label in markers
                            ),
                            None,
                        )
                        if marker_category is not None:
                            category = marker_category  # type: ignore[assignment]
                            continue
                        if label.startswith("分") and marker_category is None:
                            category = None
                            continue
                        if category is None or not label:
                            continue
                        for column, year in amount_columns.items():
                            if column >= len(row) or not row[column]:
                                continue
                            try:
                                amount, formula = convert_to_cny_million(row[column], unit)
                            except ValueError:
                                continue
                            items.append(
                                BreakdownItem(
                                    category=category,
                                    name=label,
                                    revenue_cny_million=amount,
                                    report_year=year,
                                    report_kind=document.kind,
                                    source_url=document.source_url,
                                    page_number=page.page_number,
                                    original_value=row[column],
                                    original_unit=unit,
                                    conversion_formula=formula,
                                )
                            )
                    continue

                # Interim reports and simpler tables may disclose one period with a
                # conventional single-row header.
                header = "|".join(value or "" for value in rows[0])
                category = next(
                    (name for name, markers in category_markers.items() if any(marker in header for marker in markers)),
                    None,
                )
                if category is None or "营业收入" not in header:
                    continue
                revenue_index = next(i for i, value in enumerate(rows[0]) if "营业收入" in (value or ""))
                for row in rows[1:]:
                    if len(row) <= revenue_index or not row[0] or not row[revenue_index]:
                        continue
                    try:
                        amount, formula = convert_to_cny_million(row[revenue_index], unit)
                    except ValueError:
                        continue
                    items.append(
                        BreakdownItem(
                            category=category,  # type: ignore[arg-type]
                            name=row[0].strip(),
                            revenue_cny_million=amount,
                            report_year=document.report_year,
                            report_kind=document.kind,
                            source_url=document.source_url,
                            page_number=page.page_number,
                            original_value=row[revenue_index],
                            original_unit=unit,
                            conversion_formula=formula,
                        )
                    )
    return items


def extract_customers_suppliers(document: ReportDocument) -> list[ConcentrationItem]:
    items: list[ConcentrationItem] = []
    for page in document.report.pages:
        unit = _page_unit(page.text)
        for table in page.tables:
            rows = table.rows
            if len(rows) < 2:
                continue
            header = "|".join(value or "" for value in rows[0])
            kind = "customer" if "客户" in header else "supplier" if "供应商" in header else None
            if kind is None:
                continue
            for row in rows[1:]:
                if not row or not row[0]:
                    continue
                name = row[0].strip()
                amount = None
                percent = None
                for value in row[1:]:
                    text = (value or "").strip()
                    if text.endswith("%"):
                        percent = _decimal(text[:-1])
                    elif amount is None and text and unit:
                        try:
                            amount = convert_to_cny_million(text, unit)[0]
                        except ValueError:
                            pass
                items.append(
                    ConcentrationItem(
                        kind=kind,
                        disclosed_name=name,
                        amount_cny_million=amount,
                        concentration_percent=percent,
                        source_url=document.source_url,
                        page_number=page.page_number,
                    )
                )
    return items


def extract_outlook_risks(documents: list[ReportDocument]) -> list[OutlookRiskItem]:
    items: list[OutlookRiskItem] = []
    seen: set[str] = set()
    for document in documents:
        for page in document.report.pages:
            for paragraph in re.split(r"[\r\n]+", page.text):
                text = paragraph.strip()
                if len(text) < 8 or text in seen:
                    continue
                kind = (
                    "outlook"
                    if any(marker in text for marker in ("经营计划", "未来发展", "发展战略"))
                    else "risk"
                    if any(marker in text for marker in ("风险", "不确定性"))
                    else None
                )
                if kind:
                    seen.add(text)
                    items.append(
                        OutlookRiskItem(
                            kind=kind,
                            text=text,
                            source_url=document.source_url,
                            page_number=page.page_number,
                        )
                    )
    return items


def _format_breakdown(items: list[BreakdownItem]) -> str:
    lines = []
    for item in sorted(items, key=lambda value: (value.report_year, value.report_kind, value.category, value.name), reverse=True):
        label = "Business" if item.category == "business_segment" else "Geography"
        lines.append(f"{item.report_year} {item.report_kind}: {label} - {item.name}: CNY {item.revenue_cny_million:,.2f} million")
    return "\n".join(lines)


def _format_quarters(items: list[StandaloneQuarterRecord]) -> str:
    lines = []
    for item in items:
        revenue = "Not disclosed" if item.revenue is None else f"CNY {item.revenue / Decimal('1000000'):,.2f} million"
        profit = "Not disclosed" if item.parent_net_profit is None else f"CNY {item.parent_net_profit / Decimal('1000000'):,.2f} million"
        margin = "Not disclosed" if item.gross_margin is None else f"{item.gross_margin:.2f}%"
        lines.append(f"FY{item.fiscal_year} Q{item.fiscal_quarter}: revenue {revenue}; attributable net profit {profit}; gross margin {margin}")
    return "\n".join(lines)


def collect_operating_performance(
    *,
    company: CompanyIdentity,
    raw_financial_records: list[dict[str, Any]],
    report_documents: list[ReportDocument],
    captured_at: datetime,
    outlook_english_renderer: OutlookEnglishRenderer | None = None,
) -> OperatingPerformanceResult:
    eastmoney_url = build_eastmoney_url(company)
    normalized = normalize_financial_periods(raw_financial_records, source_url=eastmoney_url)
    annual, interim = select_operating_periods(normalized)
    quarters, errors = restore_standalone_quarters(normalized)
    selected_documents = [
        item
        for item in report_documents
        if (item.kind == "annual_report" and item.report_year in {record.fiscal_year for record in annual})
        or (interim is not None and item.kind == "interim_report" and item.report_year == interim.fiscal_year)
    ]
    breakdown = extract_revenue_breakdown(selected_documents)
    allowed_annual_years = {record.fiscal_year for record in annual}
    breakdown = [
        item
        for item in breakdown
        if (
            item.report_kind == "annual_report"
            and item.report_year in allowed_annual_years
        ) or (
            interim is not None
            and item.report_kind == "interim_report"
            and item.report_year == interim.fiscal_year
        )
    ]
    # A newer annual report contains prior-year comparative columns while the
    # prior annual report contains the same year as its current column. Keep one
    # fact per disclosed period/category/name and prefer that period's own report.
    document_year_by_url = {item.source_url: item.report_year for item in selected_documents}
    deduplicated: dict[tuple[int, ReportKind, str, str], BreakdownItem] = {}
    for item in breakdown:
        key = (item.report_year, item.report_kind, item.category, item.name)
        current = deduplicated.get(key)
        if current is None or (
            document_year_by_url.get(item.source_url) == item.report_year
            and document_year_by_url.get(current.source_url) != current.report_year
        ):
            deduplicated[key] = item
    breakdown = list(deduplicated.values())
    latest_annual_year = annual[0].fiscal_year if annual else None
    latest_annual_doc = next(
        (item for item in report_documents if item.kind == "annual_report" and item.report_year == latest_annual_year),
        None,
    )
    concentration = extract_customers_suppliers(latest_annual_doc) if latest_annual_doc else []
    outlook = extract_outlook_risks(selected_documents)
    values: list[SourceValue] = []
    if breakdown:
        evidence = [
            ItemEvidence(
                item_id=f"{item.report_year}:{item.category}:{item.name}",
                source=item.report_kind,
                source_url=item.source_url,
                period=str(item.report_year),
                raw_value=item.model_dump(mode="json"),
            )
            for item in breakdown
        ]
        values.append(SourceValue(field_id="revenue_breakdown", value=_format_breakdown(breakdown), raw_value=[item.model_dump(mode="json") for item in breakdown], source=evidence[0].source, source_url=evidence[0].source_url, captured_at=captured_at, period="latest two full years and latest interim if disclosed", item_evidence=evidence))
    if quarters:
        values.append(SourceValue(field_id="quarterly_revenue_profitability", value=_format_quarters(quarters), raw_value=[item.model_dump(mode="json") for item in quarters], source="eastmoney", source_url=eastmoney_url, captured_at=captured_at, period="latest eight disclosed standalone quarters", metadata={"restoration_formulas": [item.formulas for item in quarters]}))
    if concentration and latest_annual_doc:
        lines = [f"{item.kind.title()} - {item.disclosed_name}: " + (f"CNY {item.amount_cny_million:,.2f} million" if item.amount_cny_million is not None else "amount not disclosed") + (f"; {item.concentration_percent:.2f}%" if item.concentration_percent is not None else "") for item in concentration]
        values.append(SourceValue(field_id="customers_suppliers_concentration", value="\n".join(lines), raw_value=[item.model_dump(mode="json") for item in concentration], source="annual_report", source_url=latest_annual_doc.source_url, captured_at=captured_at, period=f"FY{latest_annual_doc.report_year}", item_evidence=[ItemEvidence(item_id=f"{item.kind}:{item.disclosed_name}", source="annual_report", source_url=item.source_url, period=f"FY{latest_annual_doc.report_year}", raw_value=item.model_dump(mode="json")) for item in concentration]))
    if outlook and outlook_english_renderer is None:
        errors.append(
            "business_outlook_risks: verified disclosures found but English renderer was not provided"
        )
    elif outlook:
        rendered_outlook = outlook_english_renderer(tuple(outlook)).strip()
        if not rendered_outlook:
            raise ValueError("business_outlook_risks English renderer returned empty text")
        first_outlook_source = next(
            doc.kind for doc in selected_documents if doc.source_url == outlook[0].source_url
        )
        values.append(SourceValue(field_id="business_outlook_risks", value="\n" + rendered_outlook, raw_value=[item.model_dump(mode="json") for item in outlook], source=first_outlook_source, source_url=outlook[0].source_url, captured_at=captured_at, period="latest selected annual/interim reports", item_evidence=[ItemEvidence(item_id=f"{item.kind}:{index}", source=next(doc.kind for doc in selected_documents if doc.source_url == item.source_url), source_url=item.source_url, period=None, raw_value=item.model_dump(mode="json")) for index, item in enumerate(outlook, start=1)]))
    return OperatingPerformanceResult(source_values=values, annual_records=annual, ytd_records=normalized, quarterly_records=quarters, breakdown_items=breakdown, concentration_items=concentration, outlook_risk_items=outlook, errors=errors)


def operating_performance_node(
    state: WorkupAgentState,
    *,
    outlook_english_renderer: OutlookEnglishRenderer | None = None,
) -> dict[str, Any]:
    """Integration wrapper; inputs are supplied by the final graph integrator."""
    inputs = state.get("part_results", {}).get("part_04_input")
    if not isinstance(inputs, dict):
        raise ValueError("part_results.part_04_input is required")
    result = collect_operating_performance(
        company=CompanyIdentity.model_validate(state["company"]),
        raw_financial_records=inputs.get("raw_financial_records", []),
        report_documents=[ReportDocument.model_validate(item) for item in inputs.get("report_documents", [])],
        captured_at=datetime.fromisoformat(state["created_at"]),
        outlook_english_renderer=outlook_english_renderer,
    )
    part_results = dict(state.get("part_results", {}))
    part_results["part_04"] = result.model_dump(mode="json")
    return {
        "part_results": part_results,
        "source_values": list(state.get("source_values", []))
        + [item.model_dump(mode="json") for item in result.source_values],
        "node_errors": list(state.get("node_errors", []))
        + [{"node": "part_04", "message": message} for message in result.errors],
    }
