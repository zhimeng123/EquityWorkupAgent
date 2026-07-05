from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, HttpUrl, model_validator

from mlc_agent.schemas import DerivedEvidence, DerivedInput, ItemEvidence, SourceName, SourceValue
from mlc_agent.evidence_text import anchor_extracted_evidence, contains_normalized_evidence


PART_03_FIELD_IDS = (
    "us_subsidiary_status",
    "us_subsidiary_count",
    "us_subsidiary_details",
    "us_exposure_other_details",
    "us_revenue",
    "us_revenue_ratio",
    "us_employee_count",
    "us_employee_by_state",
)

US_STATES = frozenset(
    {
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
        "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
        "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
        "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
        "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
        "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
        "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
        "Wisconsin", "Wyoming", "District of Columbia",
        "阿拉巴马州", "阿拉斯加州", "亚利桑那州", "阿肯色州", "加利福尼亚州", "科罗拉多州",
        "康涅狄格州", "特拉华州", "佛罗里达州", "佐治亚州", "夏威夷州", "爱达荷州",
        "伊利诺伊州", "印第安纳州", "爱荷华州", "堪萨斯州", "肯塔基州", "路易斯安那州",
        "缅因州", "马里兰州", "马萨诸塞州", "密歇根州", "明尼苏达州", "密西西比州",
        "密苏里州", "蒙大拿州", "内布拉斯加州", "内华达州", "新罕布什尔州", "新泽西州",
        "新墨西哥州", "纽约州", "北卡罗来纳州", "北达科他州", "俄亥俄州", "俄克拉何马州",
        "俄勒冈州", "宾夕法尼亚州", "罗得岛州", "南卡罗来纳州", "南达科他州", "田纳西州",
        "得克萨斯州", "德克萨斯州", "犹他州", "佛蒙特州", "弗吉尼亚州", "华盛顿州",
        "西弗吉尼亚州", "威斯康星州", "怀俄明州", "哥伦比亚特区",
    }
)


class EvidenceDocument(BaseModel):
    source: Literal["annual_report", "interim_report"]
    source_url: HttpUrl
    disclosure_date: date
    period: str = Field(min_length=1)
    text: str = Field(min_length=1)


class FactEvidence(BaseModel):
    source_url: HttpUrl
    disclosure_date: date
    period: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    evidence_text: str = Field(min_length=1)


class CoverageEvidence(BaseModel):
    """Structured proof that the relevant disclosure areas were inspected."""

    source_url: HttpUrl
    disclosure_date: date
    period: str = Field(min_length=1)
    page_numbers: list[int] = Field(min_length=1)
    sections: list[str] = Field(min_length=1)
    search_dimensions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_coverage(self) -> "CoverageEvidence":
        if any(page < 1 for page in self.page_numbers):
            raise ValueError("coverage page numbers must be positive")
        if any(not value.strip() for value in self.sections + self.search_dimensions):
            raise ValueError("coverage sections and search dimensions cannot be blank")
        return self


class StatusFact(BaseModel):
    status: Literal["yes", "no", "not_disclosed"]
    evidence: FactEvidence | None = None
    coverage_evidence: CoverageEvidence | None = None

    @model_validator(mode="after")
    def validate_evidence_kind(self) -> "StatusFact":
        if self.status in {"yes", "no"}:
            if self.evidence is None or self.coverage_evidence is not None:
                raise ValueError("yes/no status requires exact fact evidence only")
        elif self.coverage_evidence is None or self.evidence is not None:
            raise ValueError("not_disclosed status requires structured coverage evidence only")
        return self


class UsSubsidiary(BaseModel):
    name: str = Field(min_length=1)
    registered_location: str = Field(min_length=1)
    country: Literal["United States", "USA", "美国"] | None = None
    us_state: str | None = None
    principal_business: str = Field(min_length=1)
    report_period: str = Field(min_length=1)
    evidence: FactEvidence

    @model_validator(mode="after")
    def validate_us_jurisdiction(self) -> "UsSubsidiary":
        if self.country is None and self.us_state not in US_STATES:
            raise ValueError("US subsidiary requires an explicit US country or canonical US state")
        if self.country is not None and self.us_state is not None and self.us_state not in US_STATES:
            raise ValueError("us_state is not a canonical US state")
        if self.report_period != self.evidence.period:
            raise ValueError("subsidiary report period must match its evidence period")
        return self


class SubsidiaryDisclosure(BaseModel):
    status: StatusFact
    count: int | None = Field(default=None, ge=0)
    subsidiaries: list[UsSubsidiary] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status(self) -> "SubsidiaryDisclosure":
        if self.status.status == "yes" and (
            self.count is None or self.count == 0 or self.count != len(self.subsidiaries)
        ):
            raise ValueError("yes subsidiary status requires matching positive count and details")
        if self.status.status == "no" and (self.count != 0 or self.subsidiaries):
            raise ValueError("no subsidiary status requires explicit zero count and no details")
        if self.status.status == "not_disclosed" and (
            self.count is not None or self.subsidiaries
        ):
            raise ValueError("not_disclosed subsidiary status cannot contain a count or details")
        return self


class DisclosureDetail(BaseModel):
    detail: str = Field(min_length=1)
    evidence: FactEvidence


class DetailDisclosure(BaseModel):
    status: StatusFact
    details: list[DisclosureDetail] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status(self) -> "DetailDisclosure":
        if self.status.status == "yes" and not self.details:
            raise ValueError("yes detail status requires details")
        if self.status.status != "yes" and self.details:
            raise ValueError("no/not_disclosed detail status cannot contain details")
        return self


class MonetaryMetric(BaseModel):
    status: StatusFact
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    period: str | None = None
    evidence: FactEvidence | None = None

    @model_validator(mode="after")
    def validate_metric(self) -> "MonetaryMetric":
        if self.status.status in {"yes", "no"}:
            if self.amount is None or not self.currency or not self.period or self.evidence is None:
                raise ValueError("disclosed monetary metric requires value, currency, period, and evidence")
            if self.status.status == "yes" and self.amount == 0:
                raise ValueError("yes monetary status requires a positive amount")
            if self.status.status == "no" and self.amount != 0:
                raise ValueError("no monetary status requires an explicit zero amount")
            if self.period != self.evidence.period:
                raise ValueError("monetary period must match evidence period")
        elif any(value is not None for value in (self.amount, self.currency, self.period, self.evidence)):
            raise ValueError("not_disclosed monetary metric cannot contain a value")
        return self


class UsRevenueMetric(MonetaryMetric):
    geographic_scope: Literal["United States"] | None = None

    @model_validator(mode="after")
    def validate_us_scope(self) -> "UsRevenueMetric":
        if self.status.status in {"yes", "no"} and self.geographic_scope != "United States":
            raise ValueError("disclosed US revenue requires explicit United States geographic scope")
        if self.status.status == "not_disclosed" and self.geographic_scope is not None:
            raise ValueError("not_disclosed US revenue cannot contain a geographic scope")
        return self


class EmployeeMetric(BaseModel):
    status: StatusFact
    count: int | None = Field(default=None, ge=0)
    period: str | None = None
    evidence: FactEvidence | None = None

    @model_validator(mode="after")
    def validate_metric(self) -> "EmployeeMetric":
        if self.status.status in {"yes", "no"}:
            if self.count is None or not self.period or self.evidence is None:
                raise ValueError("disclosed employee metric requires count, period, and evidence")
            if self.status.status == "yes" and self.count == 0:
                raise ValueError("yes employee status requires a positive count")
            if self.status.status == "no" and self.count != 0:
                raise ValueError("no employee status requires an explicit zero count")
            if self.period != self.evidence.period:
                raise ValueError("employee period must match evidence period")
        elif any(value is not None for value in (self.count, self.period, self.evidence)):
            raise ValueError("not_disclosed employee metric cannot contain a value")
        return self


class EmployeeByState(BaseModel):
    state: str = Field(min_length=1)
    count: int = Field(ge=0)
    evidence: FactEvidence

    @model_validator(mode="after")
    def validate_state(self) -> "EmployeeByState":
        if self.state not in US_STATES:
            raise ValueError("employee state is not a canonical US state")
        return self


class StateBreakdown(BaseModel):
    status: StatusFact
    items: list[EmployeeByState] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status(self) -> "StateBreakdown":
        if self.status.status == "yes" and not self.items:
            raise ValueError("yes state breakdown requires items")
        if self.status.status != "yes" and self.items:
            raise ValueError("no/not_disclosed state breakdown cannot contain items")
        return self


class UsExposureExtraction(BaseModel):
    subsidiaries: SubsidiaryDisclosure
    other_details: DetailDisclosure
    us_revenue: UsRevenueMetric
    total_revenue: MonetaryMetric
    us_employees: EmployeeMetric
    employee_by_state: StateBreakdown


class Part03Error(BaseModel):
    field_id: str
    reason: str


class UsExposureResult(BaseModel):
    source_values: list[SourceValue] = Field(default_factory=list)
    raw_result: UsExposureExtraction | None = None
    errors: list[Part03Error] = Field(default_factory=list)


class StructuredExtractor(Protocol):
    def __call__(self, documents: list[EvidenceDocument], *, as_of: date) -> dict[str, Any]: ...


_COVERAGE_SCOPE: dict[str, tuple[list[str], list[str]]] = {
    "subsidiaries": (
        ["subsidiaries and controlled entities"],
        ["United States", "USA", "美国", "US state names"],
    ),
    "other_details": (
        ["other United States exposure disclosures"],
        ["United States", "USA", "美国", "US state names"],
    ),
    "us_revenue": (
        ["geographic revenue disclosures"],
        ["United States revenue", "USA revenue", "美国收入", "美国营业收入"],
    ),
    "total_revenue": (
        ["revenue disclosures"],
        ["营业收入", "营业总收入", "revenue"],
    ),
    "us_employees": (
        ["employee disclosures"],
        ["United States employees", "USA employees", "美国员工", "美国雇员"],
    ),
    "employee_by_state": (
        ["employee geographic disclosures"],
        ["US state employee count", "美国各州员工", "美国各州雇员"],
    ),
}


def _local_not_disclosed_coverage(
    raw: dict[str, Any],
    *,
    field: str,
    documents: list[EvidenceDocument],
) -> None:
    """Replace model-authored coverage with metadata proven by the supplied input."""
    status = raw.get("status")
    if not isinstance(status, dict) or status.get("status") != "not_disclosed":
        return
    if not documents:
        return
    # The latest supplied filing is the report whose topic pages were reviewed.
    document = max(documents, key=lambda item: item.disclosure_date)
    page_numbers = sorted({
        int(value) for value in re.findall(r"\[Page (\d+)\]", document.text)
    }) or [1]
    sections, search_dimensions = _COVERAGE_SCOPE[field]
    status["evidence"] = None
    status["coverage_evidence"] = {
        "source_url": str(document.source_url),
        "disclosure_date": document.disclosure_date.isoformat(),
        "period": document.period,
        "page_numbers": page_numbers,
        "sections": sections,
        "search_dimensions": search_dimensions,
    }


def _document_for(evidence: FactEvidence, documents: list[EvidenceDocument]) -> EvidenceDocument:
    url = str(evidence.source_url)
    matches = [
        item for item in documents
        if str(item.source_url) == url
        and item.disclosure_date == evidence.disclosure_date
        and item.period == evidence.period
    ]
    if not matches:
        raise ValueError(f"evidence URL, date, or period was not supplied: {url}")
    document = matches[0]
    if not contains_normalized_evidence(document.text, evidence.evidence_text):
        raise ValueError(f"evidence text was not found in supplied document: {url}")
    return document


def _document_for_coverage(
    evidence: CoverageEvidence, documents: list[EvidenceDocument]
) -> EvidenceDocument:
    url = str(evidence.source_url)
    matches = [
        item for item in documents
        if str(item.source_url) == url
        and item.disclosure_date == evidence.disclosure_date
        and item.period == evidence.period
    ]
    if not matches:
        raise ValueError(f"coverage URL, date, or period was not supplied: {url}")
    document = matches[0]
    page_numbers = {int(value) for value in re.findall(r"\[Page (\d+)\]", document.text)} or {1}
    missing = set(evidence.page_numbers) - page_numbers
    if missing:
        raise ValueError(f"coverage pages were not supplied: {sorted(missing)}")
    return document


def _status_evidence(status: StatusFact) -> FactEvidence | CoverageEvidence:
    evidence = status.evidence or status.coverage_evidence
    assert evidence is not None
    return evidence


def _value(
    field_id: str,
    value: str,
    evidence: FactEvidence | CoverageEvidence,
    documents: list[EvidenceDocument],
    captured_at: datetime,
    raw_value: Any,
    *,
    item_evidence: list[ItemEvidence] | None = None,
    derived_evidence: DerivedEvidence | None = None,
) -> SourceValue:
    document = (
        _document_for(evidence, documents)
        if isinstance(evidence, FactEvidence)
        else _document_for_coverage(evidence, documents)
    )
    source: SourceName = "derived" if derived_evidence else document.source
    return SourceValue(
        field_id=field_id,
        value=value,
        raw_value=raw_value,
        source=source,
        source_url=str(evidence.source_url),
        captured_at=captured_at,
        period=evidence.period,
        item_evidence=item_evidence or [],
        derived_evidence=derived_evidence,
    )


def _items_evidence(items: list[Any], documents: list[EvidenceDocument]) -> list[ItemEvidence]:
    result = []
    for index, item in enumerate(items, start=1):
        document = _document_for(item.evidence, documents)
        result.append(
            ItemEvidence(
                item_id=str(index),
                source=document.source,
                source_url=str(item.evidence.source_url),
                period=item.evidence.period,
                raw_value=item.model_dump(mode="json"),
            )
        )
    return result


def _status_text(status: str) -> str:
    return {"yes": "Yes", "no": "No", "not_disclosed": "Not separately disclosed"}[status]


def collect_us_exposure(
    documents: list[EvidenceDocument],
    *,
    as_of: date,
    extractor: StructuredExtractor,
    captured_at: datetime | None = None,
) -> UsExposureResult:
    if not documents:
        return UsExposureResult(
            errors=[Part03Error(field_id=item, reason="Annual/interim report unavailable or unparsed.") for item in PART_03_FIELD_IDS]
        )
    timestamp = captured_at or datetime.now().astimezone()
    try:
        extracted = extractor(documents, as_of=as_of)
    except Exception as exc:
        return UsExposureResult(
            errors=[Part03Error(field_id="part_03_extraction", reason=str(exc))]
        )

    values: list[SourceValue] = []
    errors: list[Part03Error] = []
    validated: dict[str, Any] = {}
    specs = {
        "subsidiaries": (SubsidiaryDisclosure, ("us_subsidiary_status", "us_subsidiary_count", "us_subsidiary_details")),
        "other_details": (DetailDisclosure, ("us_exposure_other_details",)),
        "us_revenue": (UsRevenueMetric, ("us_revenue", "us_revenue_ratio")),
        "total_revenue": (MonetaryMetric, ("us_revenue_ratio",)),
        "us_employees": (EmployeeMetric, ("us_employee_count", "us_employee_by_state")),
        "employee_by_state": (StateBreakdown, ("us_employee_by_state",)),
    }
    invalid_fields: set[str] = set()
    evidence_documents = [item.model_dump(mode="json") for item in documents]
    for key, (model_type, field_ids) in specs.items():
        try:
            extraction_error = extracted.get("_extraction_errors", {}).get(key)
            if extraction_error:
                raise ValueError(extraction_error)
            anchored_block = anchor_extracted_evidence(extracted.get(key), evidence_documents)
            validated[key] = model_type.model_validate_json(
                json.dumps(anchored_block, ensure_ascii=False), strict=True
            )
        except Exception as exc:
            invalid_fields.update(field_ids)
            errors.extend(Part03Error(field_id=field_id, reason=f"{key}: {exc}") for field_id in field_ids)

    def attempt(field_id: str, build: Any) -> None:
        try:
            values.append(build())
        except Exception as exc:
            errors.append(Part03Error(field_id=field_id, reason=str(exc)))

    subsidiaries = validated.get("subsidiaries")
    if subsidiaries is not None:
        subsidiary_status = subsidiaries.status
        attempt("us_subsidiary_status", lambda: _value(
            "us_subsidiary_status", _status_text(subsidiary_status.status), _status_evidence(subsidiary_status),
            documents, timestamp, subsidiaries.model_dump(mode="json")
        ))
        count_text = "Not separately disclosed" if subsidiaries.count is None else str(subsidiaries.count)
        attempt("us_subsidiary_count", lambda: _value(
            "us_subsidiary_count", count_text, _status_evidence(subsidiary_status), documents, timestamp,
            subsidiaries.model_dump(mode="json")
        ))
        details_text = _status_text(subsidiary_status.status)
        if subsidiaries.subsidiaries:
            details_text = "\n".join(
                f"{item.name} | {item.registered_location} | {item.principal_business} | {item.report_period}"
                for item in subsidiaries.subsidiaries
            )
        attempt("us_subsidiary_details", lambda: _value(
            "us_subsidiary_details", details_text, _status_evidence(subsidiary_status), documents, timestamp,
            subsidiaries.model_dump(mode="json"),
            item_evidence=_items_evidence(subsidiaries.subsidiaries, documents)
        ))

    other = validated.get("other_details")
    if other is not None:
        other_text = _status_text(other.status.status)
        if other.details:
            other_text = "\n".join(item.detail for item in other.details)
        attempt("us_exposure_other_details", lambda: _value(
            "us_exposure_other_details", other_text, _status_evidence(other.status), documents, timestamp,
            other.model_dump(mode="json"), item_evidence=_items_evidence(other.details, documents)
        ))

    revenue = validated.get("us_revenue")
    total_revenue = validated.get("total_revenue")
    if revenue is not None:
        revenue_text = _status_text(revenue.status.status)
        revenue_evidence = revenue.evidence or _status_evidence(revenue.status)
        if revenue.status.status in {"yes", "no"}:
            assert isinstance(revenue_evidence, FactEvidence)
            normalized = revenue_evidence.evidence_text.casefold()
            if not any(marker in normalized for marker in ("美国", "美利坚", "united states", "usa", "u.s.")):
                errors.append(Part03Error(
                    field_id="us_revenue",
                    reason="US revenue evidence must explicitly identify the United States; foreign/overseas revenue is not US revenue.",
                ))
                revenue = None
        if revenue is None:
            revenue_evidence = None
    if revenue is not None:
        assert revenue_evidence is not None
        if revenue.amount is not None:
            revenue_text = f"{revenue.currency} {revenue.amount}"
        attempt("us_revenue", lambda: _value(
            "us_revenue", revenue_text, revenue_evidence, documents, timestamp,
            revenue.model_dump(mode="json")
        ))

    def build_ratio() -> SourceValue:
        if revenue is None or total_revenue is None:
            raise ValueError("US revenue or total revenue failed local validation.")
        if revenue.amount is None or total_revenue.amount is None:
            evidence = (
                _status_evidence(revenue.status)
                if revenue.amount is None
                else _status_evidence(total_revenue.status)
            )
            return _value(
                "us_revenue_ratio",
                "Not separately disclosed",
                evidence,
                documents,
                timestamp,
                {
                    "us_revenue": revenue.model_dump(mode="json"),
                    "total_revenue": total_revenue.model_dump(mode="json"),
                },
            )
        if revenue.period != total_revenue.period:
            raise ValueError("US revenue and total revenue periods do not match.")
        if revenue.currency != total_revenue.currency:
            raise ValueError("US revenue and total revenue currencies or units do not match.")
        if total_revenue.amount == 0:
            raise ValueError("Total revenue is zero.")
        assert revenue.period and revenue.evidence and total_revenue.evidence
        _document_for(revenue.evidence, documents)
        _document_for(total_revenue.evidence, documents)
        ratio = (revenue.amount / total_revenue.amount * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        derived = DerivedEvidence(
            formula="US revenue / total revenue × 100%",
            inputs=[
                DerivedInput(field_id="us_revenue", value=revenue.amount, period=revenue.period, source_url=str(revenue.evidence.source_url)),
                DerivedInput(field_id="total_revenue", value=total_revenue.amount, period=total_revenue.period, source_url=str(total_revenue.evidence.source_url)),
            ],
            result=ratio,
        )
        return _value(
            "us_revenue_ratio", f"{ratio}%", revenue.evidence, documents, timestamp,
            {"us_revenue": revenue.amount, "total_revenue": total_revenue.amount},
            derived_evidence=derived
        )

    if "us_revenue_ratio" not in invalid_fields:
        attempt("us_revenue_ratio", build_ratio)

    employees = validated.get("us_employees")
    if employees is not None:
        employee_text = _status_text(employees.status.status) if employees.count is None else str(employees.count)
        employee_evidence = employees.evidence or _status_evidence(employees.status)
        attempt("us_employee_count", lambda: _value(
            "us_employee_count", employee_text, employee_evidence, documents, timestamp,
            employees.model_dump(mode="json")
        ))

    def build_state_breakdown() -> SourceValue:
        breakdown = validated["employee_by_state"]
        assert employees is not None
        if breakdown.status.status == "yes":
            if employees.period is None:
                raise ValueError("US employee total period is not disclosed.")
            if any(item.evidence.period != employees.period for item in breakdown.items):
                raise ValueError("US employee state breakdown period does not match employee total period.")
        state_text = _status_text(breakdown.status.status)
        if breakdown.items:
            state_text = "\n".join(f"{item.state}: {item.count}" for item in breakdown.items)
        return _value(
            "us_employee_by_state", state_text, _status_evidence(breakdown.status), documents, timestamp,
            breakdown.model_dump(mode="json"), item_evidence=_items_evidence(breakdown.items, documents)
        )

    if "us_employee_by_state" not in invalid_fields:
        attempt("us_employee_by_state", build_state_breakdown)
    raw = UsExposureExtraction.model_validate(validated) if len(validated) == len(specs) else None
    return UsExposureResult(source_values=values, raw_result=raw, errors=errors)


def us_exposure_node(state: dict[str, Any], *, extractor: StructuredExtractor, as_of: date) -> dict[str, Any]:
    documents = [EvidenceDocument.model_validate(item) for item in state.get("part_03_documents", [])]
    result = collect_us_exposure(documents, as_of=as_of, extractor=extractor)
    return {
        "part_results": {"part_03": result.model_dump(mode="json")},
        "source_values": [item.model_dump(mode="json") for item in result.source_values],
        "node_errors": [item.model_dump(mode="json") for item in result.errors],
    }


def make_openai_extractor(client: Any, *, model: str) -> StructuredExtractor:
    def extract(documents: list[EvidenceDocument], *, as_of: date) -> dict[str, Any]:
        schemas: tuple[tuple[str, type[BaseModel]], ...] = (
            ("subsidiaries", SubsidiaryDisclosure),
            ("other_details", DetailDisclosure),
            ("us_revenue", UsRevenueMetric),
            ("total_revenue", MonetaryMetric),
            ("us_employees", EmployeeMetric),
            ("employee_by_state", StateBreakdown),
        )
        extracted: dict[str, Any] = {}
        extraction_errors: dict[str, str] = {}
        for key, output_model in schemas:
            response = client.chat.completions.create(
                model=model,
                messages=[
                {"role": "system", "content": (
                    "Extract US exposure only from explicit annual/interim report disclosures. Include only entities "
                    "whose country is explicitly United States/USA/美国 or whose registration names a US state. "
                    "Never treat overseas, foreign, non-US, or a generic 国外/境外 revenue figure as US revenue. "
                    "Never infer zero or a negative answer. For yes/no, provide an exact contiguous fact evidence "
                    "quote. For not_disclosed, do not invent a negative quote: provide coverage_evidence listing "
                    "the supplied report URL/date/period, inspected page_numbers, sections, and search_dimensions. "
                    "Copy an exact contiguous evidence_text substring and preserve official proper names; "
                    "copy it in the source language and never translate, abbreviate, insert ellipses, paraphrase, "
                    "or synthesize it from field values. Re-read the selected source page and verify the quotation "
                    "character-for-character before returning JSON. "
                    "Every field named status MUST be a nested JSON object, never a string. For example: "
                    "{\"status\": {\"status\": \"not_disclosed\", \"coverage_evidence\": {\"source_url\": "
                    "\"https://supplied.example/report.pdf\", \"disclosure_date\": \"2026-04-20\", "
                    "\"period\": \"FY2025\", \"page_numbers\": [1], \"sections\": [\"subsidiaries\"], "
                    "\"search_dimensions\": [\"United States/美国\"]}}}. "
                    f"Extract only the {key} object. Return only one JSON object matching this "
                    "JSON Schema exactly: "
                    + json.dumps(output_model.model_json_schema(), ensure_ascii=False)
                )},
                {"role": "user", "content": json.dumps({
                    "as_of": as_of.isoformat(),
                    "field": key,
                    "documents": [item.model_dump(mode="json") for item in documents],
                }, ensure_ascii=False)},
                ],
                temperature=0,
            )
            content = response.choices[0].message.content
            try:
                if not content:
                    raise ValueError("LLM returned an empty JSON object")
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise ValueError("LLM response must be a JSON object")
                _local_not_disclosed_coverage(
                    parsed, field=key, documents=documents
                )
                extracted[key] = parsed
            except Exception as exc:
                extraction_errors[key] = str(exc)
        extracted["_extraction_errors"] = extraction_errors
        return extracted

    return extract
