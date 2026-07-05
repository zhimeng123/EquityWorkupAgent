from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field, HttpUrl, model_validator

from mlc_agent.schemas import ItemEvidence, SourceName, SourceValue
from mlc_agent.evidence_text import anchor_extracted_evidence, contains_normalized_evidence


PART_01_FIELD_IDS = (
    "soe_classification",
    "listed_subsidiaries",
    "listed_outside_directorships",
    "major_ma_past_12m",
    "ma_plan_next_12m",
)


class EvidenceDocument(BaseModel):
    source: Literal["annual_report", "interim_report", "cninfo", "exchange"]
    source_url: HttpUrl
    disclosure_date: date
    text: str = Field(min_length=1)


class FactEvidence(BaseModel):
    source_url: HttpUrl
    disclosure_date: date
    page_number: int | None = Field(default=None, ge=1)
    evidence_text: str = Field(min_length=1)


class SoeDetermination(BaseModel):
    classification: Literal["state_owned", "non_state_owned", "not_disclosed"]
    ultimate_controller: str | None = None
    evidence: FactEvidence | None = None

    @model_validator(mode="after")
    def validate_determination(self) -> "SoeDetermination":
        if self.classification == "not_disclosed":
            if self.ultimate_controller is not None or self.evidence is None:
                raise ValueError(
                    "not_disclosed SOE status requires coverage evidence and no controller"
                )
        elif not self.ultimate_controller or self.evidence is None:
            raise ValueError("SOE determination requires controller and evidence")
        return self


class ListedSubsidiary(BaseModel):
    name: str = Field(min_length=1)
    stock_code: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    listing_status: Literal["Listed", "Trading suspended"]
    evidence: FactEvidence


class OutsideDirectorship(BaseModel):
    director_name: str = Field(min_length=1)
    is_current_director: Literal[True]
    listed_company_name: str = Field(min_length=1)
    position: str = Field(min_length=1)
    stock_code: str | None = None
    exchange: str | None = None
    evidence: FactEvidence


class MajorMaItem(BaseModel):
    announcement_date: date
    transaction_target: str = Field(min_length=1)
    country: str = Field(min_length=1)
    target_listed: Literal["Yes", "No", "Not disclosed"]
    consideration: str = Field(min_length=1)
    status: str = Field(min_length=1)
    evidence: FactEvidence


ItemT = TypeVar("ItemT")


class DisclosureSection(BaseModel, Generic[ItemT]):
    status: Literal["yes", "no", "not_disclosed"]
    items: list[ItemT] = Field(default_factory=list)
    negative_evidence: FactEvidence | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "DisclosureSection":
        if self.status == "yes" and not self.items:
            raise ValueError("yes status requires at least one item")
        if self.status == "no" and (self.items or self.negative_evidence is None):
            raise ValueError("no status requires explicit negative evidence and no items")
        if self.status == "not_disclosed" and (self.items or self.negative_evidence is None):
            raise ValueError("not_disclosed status requires coverage evidence and no items")
        return self


class CompanySupplementExtraction(BaseModel):
    soe: SoeDetermination
    listed_subsidiaries: DisclosureSection[ListedSubsidiary]
    outside_directorships: DisclosureSection[OutsideDirectorship]
    major_ma_past_12m: DisclosureSection[MajorMaItem]
    ma_plan_next_12m: DisclosureSection[MajorMaItem]


class Part01Error(BaseModel):
    field_id: str
    reason: str


class CompanySupplementResult(BaseModel):
    source_values: list[SourceValue] = Field(default_factory=list)
    raw_result: CompanySupplementExtraction | None = None
    errors: list[Part01Error] = Field(default_factory=list)


class StructuredExtractor(Protocol):
    def __call__(self, documents: list[EvidenceDocument], *, as_of: date) -> dict[str, Any]: ...


def _source_for(url: str, documents: list[EvidenceDocument]) -> SourceName:
    for document in documents:
        if str(document.source_url) == url:
            return document.source
    raise ValueError(f"evidence URL was not present in supplied documents: {url}")


def _validate_evidence(evidence: FactEvidence, documents: list[EvidenceDocument]) -> SourceName:
    url = str(evidence.source_url)
    source = _source_for(url, documents)
    document = next(item for item in documents if str(item.source_url) == url)
    if evidence.disclosure_date != document.disclosure_date:
        raise ValueError(f"evidence date does not match supplied document: {url}")
    if not contains_normalized_evidence(document.text, evidence.evidence_text):
        raise ValueError(f"evidence text was not found in supplied document: {url}")
    return source


def _format_evidence_items(items: list[Any], documents: list[EvidenceDocument]) -> list[ItemEvidence]:
    result: list[ItemEvidence] = []
    for index, item in enumerate(items, start=1):
        source = _validate_evidence(item.evidence, documents)
        result.append(
            ItemEvidence(
                item_id=str(index),
                source=source,
                source_url=str(item.evidence.source_url),
                period=item.evidence.disclosure_date.isoformat(),
                raw_value=item.model_dump(mode="json"),
            )
        )
    return result


def _source_value(
    *,
    field_id: str,
    value: str,
    evidence: FactEvidence,
    documents: list[EvidenceDocument],
    captured_at: datetime,
    raw_value: Any,
    item_evidence: list[ItemEvidence] | None = None,
) -> SourceValue:
    source = _validate_evidence(evidence, documents)
    return SourceValue(
        field_id=field_id,
        value=value,
        raw_value=raw_value,
        source=source,
        source_url=str(evidence.source_url),
        captured_at=captured_at,
        period=evidence.disclosure_date.isoformat(),
        item_evidence=item_evidence or [],
    )


def _format_subsidiaries(items: list[ListedSubsidiary]) -> str:
    return "\n".join(
        f"{item.listing_status} | {item.name} | {item.stock_code} | {item.exchange}"
        for item in items
    )


def _listed_subsidiary_is_explicit(item: ListedSubsidiary) -> bool:
    """Require the quotation itself to establish every listing attribute.

    A major-subsidiary table establishes control and financial importance, not
    that the subsidiary is separately listed.  Listing status therefore cannot
    be inferred from such a table.
    """
    text = re.sub(r"\s+", "", item.evidence.evidence_text)
    exchange = re.sub(r"\s+", "", item.exchange)
    return (
        re.sub(r"\s+", "", item.name) in text
        and re.sub(r"\s+", "", item.stock_code) in text
        and exchange in text
        and any(marker in text for marker in ("上市", "挂牌", "暂停交易", "停牌"))
    )


def _format_directorships(items: list[OutsideDirectorship]) -> str:
    lines = []
    for item in items:
        market = " | ".join(value for value in (item.stock_code, item.exchange) if value)
        suffix = f" | {market}" if market else ""
        lines.append(
            f"{item.director_name} | {item.listed_company_name} | {item.position}{suffix}"
        )
    return "\n".join(lines)


def _format_ma(items: list[MajorMaItem]) -> str:
    return "\n".join(
        " | ".join(
            (
                item.announcement_date.isoformat(),
                item.transaction_target,
                item.country,
                f"Listed: {item.target_listed}",
                f"Consideration: {item.consideration}",
                f"Status: {item.status}",
            )
        )
        for item in items
    )


def _section_source_value(
    *,
    field_id: str,
    section: DisclosureSection[Any],
    formatter: Callable[[list[Any]], str],
    documents: list[EvidenceDocument],
    captured_at: datetime,
) -> SourceValue | Part01Error:
    if section.status == "not_disclosed":
        assert section.negative_evidence is not None
        return _source_value(
            field_id=field_id,
            value="Not disclosed",
            evidence=section.negative_evidence,
            documents=documents,
            captured_at=captured_at,
            raw_value=section.model_dump(mode="json"),
        )
    if section.status == "no":
        assert section.negative_evidence is not None
        return _source_value(
            field_id=field_id,
            value="No",
            evidence=section.negative_evidence,
            documents=documents,
            captured_at=captured_at,
            raw_value=section.model_dump(mode="json"),
        )
    evidence = section.items[0].evidence
    return _source_value(
        field_id=field_id,
        value=formatter(section.items),
        evidence=evidence,
        documents=documents,
        captured_at=captured_at,
        raw_value=section.model_dump(mode="json"),
        item_evidence=_format_evidence_items(section.items, documents),
    )


def collect_company_supplement(
    documents: list[EvidenceDocument],
    *,
    as_of: date,
    extractor: StructuredExtractor,
    captured_at: datetime | None = None,
) -> CompanySupplementResult:
    timestamp = captured_at or datetime.now().astimezone()
    try:
        extracted = extractor(documents, as_of=as_of)
        if not isinstance(extracted, dict):
            raise ValueError("company supplement extraction must be a JSON object")
    except Exception as exc:
        return CompanySupplementResult(
            errors=[Part01Error(field_id="part_01_extraction", reason=str(exc))]
        )

    values: list[SourceValue] = []
    errors: list[Part01Error] = []
    parsed: dict[str, Any] = {}
    evidence_documents = [item.model_dump(mode="json") for item in documents]
    try:
        extraction_error = extracted.get("_extraction_errors", {}).get("soe")
        if extraction_error:
            raise ValueError(extraction_error)
        anchored_soe = anchor_extracted_evidence(extracted.get("soe"), evidence_documents)
        soe = SoeDetermination.model_validate_json(
            json.dumps(anchored_soe, ensure_ascii=False), strict=True
        )
        parsed["soe"] = soe
        if soe.classification == "not_disclosed":
            assert soe.evidence is not None
            values.append(
                _source_value(
                    field_id="soe_classification",
                    value="Not disclosed",
                    evidence=soe.evidence,
                    documents=documents,
                    captured_at=timestamp,
                    raw_value=soe.model_dump(mode="json"),
                )
            )
        else:
            assert soe.evidence is not None and soe.ultimate_controller is not None
            label = "SOE" if soe.classification == "state_owned" else "Non-SOE"
            values.append(
                _source_value(
                    field_id="soe_classification",
                    value=f"{label} | Ultimate controller: {soe.ultimate_controller}",
                    evidence=soe.evidence,
                    documents=documents,
                    captured_at=timestamp,
                    raw_value=soe.model_dump(mode="json"),
                )
            )
    except Exception as exc:
        errors.append(Part01Error(field_id="soe_classification", reason=str(exc)))

    section_specs = (
        ("listed_subsidiaries", "listed_subsidiaries", ListedSubsidiary, _format_subsidiaries),
        ("listed_outside_directorships", "outside_directorships", OutsideDirectorship, _format_directorships),
        ("major_ma_past_12m", "major_ma_past_12m", MajorMaItem, _format_ma),
        ("ma_plan_next_12m", "ma_plan_next_12m", MajorMaItem, _format_ma),
    )
    for field_id, extraction_key, item_model, formatter in section_specs:
        try:
            extraction_error = extracted.get("_extraction_errors", {}).get(extraction_key)
            if extraction_error:
                raise ValueError(extraction_error)
            section_model = DisclosureSection[item_model]
            anchored_section = anchor_extracted_evidence(
                extracted.get(extraction_key), evidence_documents
            )
            section = section_model.model_validate_json(
                json.dumps(anchored_section, ensure_ascii=False), strict=True
            )
            if field_id == "listed_subsidiaries" and section.status == "yes":
                unsupported = [
                    item for item in section.items if not _listed_subsidiary_is_explicit(item)
                ]
                if unsupported:
                    coverage = unsupported[0].evidence
                    if not re.search(r"子公司|控股参股公司", coverage.evidence_text):
                        raise ValueError(
                            "listed subsidiary evidence must explicitly disclose the name, "
                            "stock code, exchange and listing status"
                        )
                    section = section_model.model_validate(
                        {
                            "status": "not_disclosed",
                            "items": [],
                            "negative_evidence": coverage.model_dump(mode="json"),
                        }
                    )
            parsed[extraction_key] = section
            if field_id in {"major_ma_past_12m", "ma_plan_next_12m"}:
                if any(item.announcement_date > as_of for item in section.items):
                    raise ValueError("M&A disclosure date cannot be after the run date")
            if field_id == "major_ma_past_12m":
                try:
                    window_start = as_of.replace(year=as_of.year - 1)
                except ValueError:
                    window_start = as_of.replace(year=as_of.year - 1, day=28)
                if any(
                    not window_start <= item.announcement_date <= as_of
                    for item in section.items
                ):
                    raise ValueError("past-12-month M&A item is outside the inclusive window")
            result = _section_source_value(
                field_id=field_id,
                section=section,
                formatter=formatter,
                documents=documents,
                captured_at=timestamp,
            )
            (errors if isinstance(result, Part01Error) else values).append(result)
        except Exception as exc:
            errors.append(Part01Error(field_id=field_id, reason=str(exc)))
    raw = None
    if set(parsed) == {
        "soe",
        "listed_subsidiaries",
        "outside_directorships",
        "major_ma_past_12m",
        "ma_plan_next_12m",
    }:
        raw = CompanySupplementExtraction.model_validate(parsed)
    return CompanySupplementResult(source_values=values, raw_result=raw, errors=errors)


def company_supplement_node(
    state: dict[str, Any], *, extractor: StructuredExtractor, as_of: date
) -> dict[str, Any]:
    documents = [EvidenceDocument.model_validate(item) for item in state.get("part_01_documents", [])]
    result = collect_company_supplement(documents, as_of=as_of, extractor=extractor)
    return {
        "part_results": {"part_01": result.model_dump(mode="json")},
        "source_values": [item.model_dump(mode="json") for item in result.source_values],
        "node_errors": [item.model_dump(mode="json") for item in result.errors],
    }


def make_openai_extractor(client: Any, *, model: str) -> StructuredExtractor:
    def extract(documents: list[EvidenceDocument], *, as_of: date) -> dict[str, Any]:
        schemas: tuple[tuple[str, type[BaseModel]], ...] = (
            ("soe", SoeDetermination),
            ("listed_subsidiaries", DisclosureSection[ListedSubsidiary]),
            ("outside_directorships", DisclosureSection[OutsideDirectorship]),
            ("major_ma_past_12m", DisclosureSection[MajorMaItem]),
            ("ma_plan_next_12m", DisclosureSection[MajorMaItem]),
        )
        extracted: dict[str, Any] = {}
        extraction_errors: dict[str, str] = {}
        for key, output_model in schemas:
            response = client.chat.completions.create(
                model=model,
                messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract only explicitly disclosed facts. SOE is determined only by whether the "
                        "ultimate actual controller is state-owned: state_owned only when the final actual "
                        "controller is explicitly state-owned; non_state_owned only when an explicitly named "
                        "final actual controller is not state-owned; use not_disclosed when there is no actual "
                        "controller or no controller can be identified. For state_owned and non_state_owned, "
                        "ultimate_controller and evidence are mandatory. For not_disclosed ultimate_controller "
                        "must be null but evidence is mandatory "
                        "and must prove the searched topic scope. Every DisclosureSection with status "
                        "not_disclosed must put that coverage evidence in negative_evidence. "
                        "For listed_subsidiaries, a major/controlled subsidiary table alone does not prove "
                        "that a subsidiary is separately listed. Return yes only when the supplied quotation "
                        "explicitly states the subsidiary name, stock code, exchange and listing status. If "
                        "the subsidiary topic was reviewed but those listing attributes are absent, return "
                        "not_disclosed with that topic quotation as coverage; exchange verification is then required. "
                        "Outside directorships include current "
                        "directors only. Do not infer names, relationships, negative answers, translations, "
                        "or missing values. Include an exact contiguous evidence_text copied from the supplied "
                        "document in its source language; never translate, abbreviate, insert ellipses, paraphrase, "
                        "or synthesize it from field values. Re-read the source and verify the quotation "
                        "character-for-character before returning JSON. "
                        f"Extract only the {key} object. Return only one JSON object matching "
                        "this JSON Schema exactly: "
                        + json.dumps(output_model.model_json_schema(), ensure_ascii=False)
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "as_of": as_of.isoformat(),
                            "field": key,
                            "documents": [item.model_dump(mode="json") for item in documents],
                        },
                        ensure_ascii=False,
                    ),
                },
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
                extracted[key] = parsed
            except Exception as exc:
                extraction_errors[key] = str(exc)
        extracted["_extraction_errors"] = extraction_errors
        return extracted

    return extract
