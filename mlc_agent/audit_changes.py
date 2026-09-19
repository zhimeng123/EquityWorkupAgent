from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from mlc_agent.litigation import (
    LegalRegulatoryMatter,
    MatterDisclosure,
    MatterEvidence,
    format_matters,
    select_in_scope_matters,
    subtract_years,
)
from mlc_agent.news import NewsArticle, NewsEvent, build_negative_news_attachment, select_and_deduplicate_news
from mlc_agent.schemas import ItemEvidence, SourceValue
from mlc_agent.evidence_text import contains_normalized_evidence


PART_11_FIELD_IDS = (
    "auditor_profile", "audit_opinion", "qualified_opinion_details",
    "auditor_change_status", "auditor_change_details",
    "financial_restatement_status", "financial_restatement_details",
    "board_officer_material_change", "business_operation_material_change",
    "top3_shareholder_change", "nonfinancial_change_details",
    "litigation_status", "regulatory_status", "litigation_regulatory_details",
    "negative_news_summary",
)

BIG4_ALIASES = {
    "Deloitte": ("deloitte", "德勤华永"),
    "PwC": ("pricewaterhousecoopers", "pwc", "普华永道中天"),
    "EY": ("ernstyoung", "ey", "安永华明"),
    "KPMG": ("kpmg", "毕马威华振"),
}

EXPLICIT_NEGATIVE_MARKERS = (
    "不存在", "未发生", "未出现", "没有", "无重大", "不涉及", "不适用",
    "无会计政策、会计估计变更或重大会计差错更正",
    "none", "no material", "did not occur", "not applicable",
)


def has_explicit_negative_statement(text: str) -> bool:
    """Return true only for a quotation that itself states the negative conclusion."""
    normalized = " ".join(text.casefold().split())
    return any(marker in normalized for marker in EXPLICIT_NEGATIVE_MARKERS)


class EvidenceDocument(BaseModel):
    source: Literal["annual_report", "interim_report", "cninfo", "exchange"]
    source_url: HttpUrl
    disclosure_date: date
    period: str = Field(min_length=1)
    text: str = Field(min_length=1)
    pages: list[dict[str, Any]] = Field(default_factory=list)


class MatterEvidenceDocument(BaseModel):
    source: Literal["cninfo", "exchange"]
    source_url: HttpUrl
    disclosure_date: date
    text: str = Field(min_length=1)
    pages: list[dict[str, Any]] = Field(default_factory=list)


class FactEvidence(BaseModel):
    source: Literal["annual_report", "interim_report", "cninfo", "exchange"]
    source_url: HttpUrl
    disclosure_date: date
    period: str = Field(min_length=1)
    evidence_text: str = Field(min_length=1)
    page_number: int = Field(ge=1)


class AuditRecord(BaseModel):
    fiscal_year: int
    auditor_name: str = Field(min_length=1)
    non_big4_background: str | None = None
    opinion: Literal["Unmodified", "Qualified", "Adverse", "Disclaimer"]
    modified_opinion_details: str | None = None
    evidence: FactEvidence

    @model_validator(mode="after")
    def validate_details(self) -> "AuditRecord":
        if self.opinion == "Unmodified" and self.modified_opinion_details is not None:
            raise ValueError("unmodified opinion cannot contain modified-opinion details")
        if self.opinion != "Unmodified" and not self.modified_opinion_details:
            raise ValueError("modified audit opinion requires details")
        return self


class AuditorChangeEvent(BaseModel):
    effective_date: date
    former_auditor: str = Field(min_length=1)
    new_auditor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: FactEvidence


class RestatementEvent(BaseModel):
    event_date: date
    periods_affected: str = Field(min_length=1)
    details: str = Field(min_length=1)
    evidence: FactEvidence


class RestatementDisclosure(BaseModel):
    status: Literal["yes", "no", "not_disclosed"]
    events: list[RestatementEvent] = Field(default_factory=list)
    negative_evidence: FactEvidence | None = None
    coverage_evidence: FactEvidence | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "RestatementDisclosure":
        if self.status == "yes" and not self.events:
            raise ValueError("yes restatement status requires events")
        if self.status == "no" and (self.events or self.negative_evidence is None):
            raise ValueError("No restatement status requires explicit negative evidence")
        if (
            self.status == "no"
            and self.negative_evidence is not None
            and not has_explicit_negative_statement(self.negative_evidence.evidence_text)
        ):
            raise ValueError("No restatement status requires an explicitly negative quotation")
        if self.status == "not_disclosed" and (
            self.events or self.negative_evidence is not None or self.coverage_evidence is None
        ):
            raise ValueError("not_disclosed restatement status requires coverage evidence")
        return self


class MaterialChangeEvent(BaseModel):
    category: Literal["board_officer", "business_operation", "top3_shareholder"]
    event_date: date
    details: str = Field(min_length=1)
    explicit_material: Literal[True]
    evidence: FactEvidence


class MaterialChangeDisclosure(BaseModel):
    status: Literal["yes", "no", "not_disclosed"]
    events: list[MaterialChangeEvent] = Field(default_factory=list)
    negative_evidence: FactEvidence | None = None
    coverage_evidence: FactEvidence | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "MaterialChangeDisclosure":
        if self.status == "yes" and not self.events:
            raise ValueError("yes material-change status requires events")
        if self.status == "no" and (self.events or self.negative_evidence is None):
            raise ValueError("No material-change status requires explicit negative evidence")
        if (
            self.status == "no"
            and self.negative_evidence is not None
            and not has_explicit_negative_statement(self.negative_evidence.evidence_text)
        ):
            raise ValueError("No material-change status requires an explicitly negative quotation")
        if self.status == "not_disclosed" and (
            self.events or self.negative_evidence is not None or self.coverage_evidence is None
        ):
            raise ValueError("not_disclosed material-change status requires coverage evidence")
        return self


class Part11Input(BaseModel):
    documents: list[EvidenceDocument]
    matter_documents: list[MatterEvidenceDocument] = Field(default_factory=list)
    latest_audit: AuditRecord
    previous_audit: AuditRecord | None = None
    post_report_auditor_changes: list[AuditorChangeEvent] = Field(default_factory=list)
    restatements: RestatementDisclosure
    board_officer_changes: MaterialChangeDisclosure
    business_operation_changes: MaterialChangeDisclosure
    top3_shareholder_changes: MaterialChangeDisclosure
    litigation: MatterDisclosure
    regulatory: MatterDisclosure
    news_articles: list[NewsArticle] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_audit_year_sequence(self) -> "Part11Input":
        if self.previous_audit is not None and self.previous_audit.fiscal_year != self.latest_audit.fiscal_year - 1:
            raise ValueError("audit comparison requires two consecutive full fiscal years")
        return self


class Part11Error(BaseModel):
    field_id: str
    reason: str


class Part11Artifact(BaseModel):
    field_id: Literal["negative_news_attachment"] = "negative_news_attachment"
    artifact_type: Literal["docx"] = "docx"
    path: str


class Part11Result(BaseModel):
    status: Literal["completed", "partial", "failed"]
    source_values: list[SourceValue] = Field(default_factory=list)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[Part11Artifact] = Field(default_factory=list)
    errors: list[Part11Error] = Field(default_factory=list)


def identify_big4(auditor_name: str) -> str | None:
    normalized = "".join(character for character in auditor_name.casefold() if character.isalnum())
    for firm, aliases in BIG4_ALIASES.items():
        if any("".join(character for character in alias.casefold() if character.isalnum()) in normalized for alias in aliases):
            return firm
    return None


def _document_for(evidence: FactEvidence, documents: list[EvidenceDocument]) -> EvidenceDocument:
    matches = [item for item in documents if (
        str(item.source_url) == str(evidence.source_url)
        and item.source == evidence.source
        and item.disclosure_date == evidence.disclosure_date
        and item.period == evidence.period
    )]
    if not matches:
        raise ValueError(f"evidence URL/date/period was not supplied: {evidence.source_url}")
    if not contains_normalized_evidence(matches[0].text, evidence.evidence_text):
        raise ValueError(f"evidence text was not found in supplied document: {evidence.source_url}")
    return matches[0]


def _matter_document_for(
    evidence: MatterEvidence,
    documents: list[EvidenceDocument | MatterEvidenceDocument],
) -> EvidenceDocument | MatterEvidenceDocument:
    matches = [item for item in documents if (
        str(item.source_url) == str(evidence.source_url)
        and item.source == evidence.source
        and item.disclosure_date == evidence.disclosure_date
    )]
    if not matches or not contains_normalized_evidence(matches[0].text, evidence.evidence_text):
        raise ValueError(f"matter evidence was not found in supplied documents: {evidence.source_url}")
    return matches[0]


def _value(
    field_id: str,
    value: str,
    evidence: FactEvidence,
    documents: list[EvidenceDocument],
    captured_at: datetime,
    raw: Any,
    *,
    items: list[ItemEvidence] | None = None,
) -> SourceValue:
    _document_for(evidence, documents)
    return SourceValue(
        field_id=field_id, value=value, raw_value=raw, source=evidence.source,
        source_url=str(evidence.source_url), captured_at=captured_at, period=evidence.period,
        metadata={"page": evidence.page_number} if evidence.page_number else {},
        item_evidence=items or [],
    )


def _fact_items(events: list[Any], documents: list[EvidenceDocument]) -> list[ItemEvidence]:
    output = []
    for index, event in enumerate(events, start=1):
        _document_for(event.evidence, documents)
        output.append(ItemEvidence(
            item_id=str(index), source=event.evidence.source,
            source_url=str(event.evidence.source_url), period=event.evidence.period,
            raw_value=event.model_dump(mode="json"),
        ))
    return output


def _matter_items(
    events: list[LegalRegulatoryMatter],
    documents: list[EvidenceDocument | MatterEvidenceDocument],
) -> list[ItemEvidence]:
    output = []
    for index, event in enumerate(events, start=1):
        document = _matter_document_for(event.evidence, documents)
        output.append(ItemEvidence(
            item_id=str(index), source=document.source, source_url=str(event.evidence.source_url),
            period=event.evidence.disclosure_date.isoformat(), raw_value=event.model_dump(mode="json"),
        ))
    return output


def collect_part11(
    data: Part11Input,
    *,
    company_name: str,
    as_of: date,
    captured_at: datetime,
    artifact_dir: Path,
) -> Part11Result:
    matter_documents = data.documents + data.matter_documents
    values: list[SourceValue] = []
    errors: list[Part11Error] = []
    artifacts: list[Part11Artifact] = []

    def attempt(field_id: str, builder: Any) -> None:
        try:
            values.append(builder())
        except Exception as exc:
            errors.append(Part11Error(field_id=field_id, reason=str(exc)))

    big4 = identify_big4(data.latest_audit.auditor_name)
    if big4 is None and not data.latest_audit.non_big4_background:
        errors.append(Part11Error(field_id="auditor_profile", reason="Non-Big 4 auditor background was not disclosed."))
    else:
        profile = (
            f"{data.latest_audit.auditor_name} | Big 4: Yes ({big4})"
            if big4 else
            f"{data.latest_audit.auditor_name} | Big 4: No | Background: {data.latest_audit.non_big4_background}"
        )
        attempt("auditor_profile", lambda: _value(
            "auditor_profile", profile, data.latest_audit.evidence, data.documents, captured_at,
            data.latest_audit.model_dump(mode="json")
        ))
    attempt("audit_opinion", lambda: _value(
        "audit_opinion", data.latest_audit.opinion, data.latest_audit.evidence,
        data.documents, captured_at, data.latest_audit.model_dump(mode="json")
    ))
    opinion_details = data.latest_audit.modified_opinion_details or "N/A - standard unqualified opinion"
    attempt("qualified_opinion_details", lambda: _value(
        "qualified_opinion_details", opinion_details, data.latest_audit.evidence,
        data.documents, captured_at, data.latest_audit.model_dump(mode="json")
    ))

    change_events = [
        item for item in data.post_report_auditor_changes
        if data.latest_audit.evidence.disclosure_date < item.effective_date <= as_of
    ]
    if data.previous_audit is None:
        errors.extend([
            Part11Error(field_id="auditor_change_status", reason="Previous full-year audit record is unavailable."),
            Part11Error(field_id="auditor_change_details", reason="Previous full-year audit record is unavailable."),
        ])
    else:
        try:
            _document_for(data.previous_audit.evidence, data.documents)
            changed = data.previous_audit.auditor_name != data.latest_audit.auditor_name or bool(change_events)
            change_evidence = change_events[0].evidence if change_events else data.latest_audit.evidence
            details = [
                f"FY{data.previous_audit.fiscal_year} {data.previous_audit.auditor_name} -> "
                f"FY{data.latest_audit.fiscal_year} {data.latest_audit.auditor_name}"
            ] if data.previous_audit.auditor_name != data.latest_audit.auditor_name else []
            details.extend(
                f"{item.effective_date.isoformat()} | {item.former_auditor} -> {item.new_auditor} | {item.reason}"
                for item in change_events
            )
            attempt("auditor_change_status", lambda: _value(
                "auditor_change_status", "Yes" if changed else "No", change_evidence,
                data.documents, captured_at, {"events": [item.model_dump(mode="json") for item in change_events]}
            ))
            attempt("auditor_change_details", lambda: _value(
                "auditor_change_details", "\n".join(details) if details else "Not applicable",
                change_evidence, data.documents, captured_at,
                {"events": [item.model_dump(mode="json") for item in change_events]},
                items=_fact_items(change_events, data.documents)
            ))
        except Exception as exc:
            errors.extend([
                Part11Error(field_id="auditor_change_status", reason=str(exc)),
                Part11Error(field_id="auditor_change_details", reason=str(exc)),
            ])

    restatement = data.restatements
    if restatement.status == "not_disclosed":
        assert restatement.coverage_evidence is not None
        attempt("financial_restatement_status", lambda: _value(
            "financial_restatement_status", "Not disclosed", restatement.coverage_evidence,
            data.documents, captured_at, restatement.model_dump(mode="json")
        ))
        attempt("financial_restatement_details", lambda: _value(
            "financial_restatement_details", "Not disclosed", restatement.coverage_evidence,
            data.documents, captured_at, restatement.model_dump(mode="json")
        ))
    else:
        window = subtract_years(as_of, 2)
        events = [item for item in restatement.events if window <= item.event_date <= as_of]
        if restatement.status == "yes" and not events:
            errors.extend([
                Part11Error(field_id="financial_restatement_status", reason="No disclosed restatement falls in the 24-month window."),
                Part11Error(field_id="financial_restatement_details", reason="No disclosed restatement falls in the 24-month window."),
            ])
        else:
            evidence = events[0].evidence if events else restatement.negative_evidence
            assert evidence is not None
            detail = "\n".join(
                f"{item.event_date.isoformat()} | {item.periods_affected} | {item.details}" for item in events
            ) or "Not applicable"
            attempt("financial_restatement_status", lambda: _value(
                "financial_restatement_status", "Yes" if events else "No", evidence,
                data.documents, captured_at, restatement.model_dump(mode="json")
            ))
            attempt("financial_restatement_details", lambda: _value(
                "financial_restatement_details", detail, evidence, data.documents, captured_at,
                restatement.model_dump(mode="json"), items=_fact_items(events, data.documents)
            ))

    material_sections = (
        ("board_officer_material_change", "board_officer", data.board_officer_changes),
        ("business_operation_material_change", "business_operation", data.business_operation_changes),
        ("top3_shareholder_change", "top3_shareholder", data.top3_shareholder_changes),
    )
    accepted_material: list[MaterialChangeEvent] = []
    for field_id, category, disclosure in material_sections:
        if any(item.category != category for item in disclosure.events):
            errors.append(Part11Error(field_id=field_id, reason=f"{category} disclosure contains another category."))
            continue
        events = [item for item in disclosure.events if item.event_date <= as_of]
        if disclosure.status == "not_disclosed":
            assert disclosure.coverage_evidence is not None
            attempt(field_id, lambda field_id=field_id, disclosure=disclosure: _value(
                field_id, "Not disclosed", disclosure.coverage_evidence,
                data.documents, captured_at, disclosure.model_dump(mode="json")
            ))
            continue
        evidence = events[0].evidence if events else disclosure.negative_evidence
        if evidence is None:
            errors.append(Part11Error(field_id=field_id, reason="Material change evidence is missing."))
            continue
        accepted_material.extend(events)
        attempt(field_id, lambda field_id=field_id, disclosure=disclosure, evidence=evidence, events=events: _value(
            field_id, "Yes" if events else "No", evidence, data.documents, captured_at,
            disclosure.model_dump(mode="json"), items=_fact_items(events, data.documents)
        ))
    if accepted_material:
        first = sorted(accepted_material, key=lambda item: item.event_date)[0]
        attempt("nonfinancial_change_details", lambda: _value(
            "nonfinancial_change_details",
            "\n".join(f"{item.event_date.isoformat()} | {item.category} | {item.details}" for item in sorted(accepted_material, key=lambda item: item.event_date)),
            first.evidence, data.documents, captured_at,
            [item.model_dump(mode="json") for item in accepted_material],
            items=_fact_items(accepted_material, data.documents)
        ))
    else:
        evidence = next((section.negative_evidence for _, _, section in material_sections if section.negative_evidence), None)
        if evidence:
            attempt("nonfinancial_change_details", lambda: _value(
                "nonfinancial_change_details", "Not applicable", evidence, data.documents,
                captured_at, []
            ))
        else:
            coverage = next(
                (section.coverage_evidence for _, _, section in material_sections if section.coverage_evidence),
                None,
            )
            if coverage:
                attempt("nonfinancial_change_details", lambda: _value(
                    "nonfinancial_change_details", "Not disclosed", coverage,
                    data.documents, captured_at, []
                ))
            else:
                errors.append(Part11Error(field_id="nonfinancial_change_details", reason="No verified material-change detail evidence."))

    selected_by_kind: dict[str, list[LegalRegulatoryMatter]] = {}
    for field_id, kind, disclosure in (
        ("litigation_status", "litigation", data.litigation),
        ("regulatory_status", "regulatory", data.regulatory),
    ):
        if disclosure.status == "not_disclosed":
            assert disclosure.coverage_evidence is not None
            try:
                document = _matter_document_for(disclosure.coverage_evidence, matter_documents)
                values.append(SourceValue(
                    field_id=field_id,
                    value="Not disclosed",
                    raw_value=disclosure.model_dump(mode="json"),
                    source=document.source,
                    source_url=str(disclosure.coverage_evidence.source_url),
                    captured_at=captured_at,
                    period=disclosure.coverage_evidence.disclosure_date.isoformat(),
                ))
            except Exception as exc:
                errors.append(Part11Error(field_id=field_id, reason=str(exc)))
            selected_by_kind[kind] = []
            continue
        try:
            selected = select_in_scope_matters(disclosure, kind=kind, as_of=as_of)
            selected_by_kind[kind] = selected
            evidence = selected[0].evidence if selected else disclosure.negative_evidence
            assert evidence is not None
            document = _matter_document_for(evidence, matter_documents)
            values.append(SourceValue(
                field_id=field_id, value="Yes" if selected else "No",
                raw_value=disclosure.model_dump(mode="json"), source=document.source,
                source_url=str(evidence.source_url), captured_at=captured_at,
                period=evidence.disclosure_date.isoformat(),
                item_evidence=_matter_items(selected, matter_documents),
            ))
        except Exception as exc:
            errors.append(Part11Error(field_id=field_id, reason=str(exc)))
            selected_by_kind[kind] = []
    all_matters = selected_by_kind.get("litigation", []) + selected_by_kind.get("regulatory", [])
    if all_matters:
        try:
            first = sorted(all_matters, key=lambda item: item.event_date, reverse=True)[0]
            document = _matter_document_for(first.evidence, matter_documents)
            values.append(SourceValue(
                field_id="litigation_regulatory_details", value=format_matters(sorted(all_matters, key=lambda item: item.event_date, reverse=True)),
                raw_value=[item.model_dump(mode="json") for item in all_matters], source=document.source,
                source_url=str(first.evidence.source_url), captured_at=captured_at,
                period="latest pending and rolling 24 months", item_evidence=_matter_items(all_matters, matter_documents),
            ))
        except Exception as exc:
            errors.append(Part11Error(field_id="litigation_regulatory_details", reason=str(exc)))
    elif data.litigation.status == "no" and data.regulatory.status == "no":
        evidence = data.litigation.negative_evidence
        assert evidence is not None
        try:
            document = _matter_document_for(evidence, matter_documents)
            values.append(SourceValue(
                field_id="litigation_regulatory_details", value="Not applicable", raw_value=[],
                source=document.source, source_url=str(evidence.source_url), captured_at=captured_at,
                period=evidence.disclosure_date.isoformat(),
            ))
        except Exception as exc:
            errors.append(Part11Error(field_id="litigation_regulatory_details", reason=str(exc)))
    elif (
        {data.litigation.status, data.regulatory.status} <= {"no", "not_disclosed"}
        and "not_disclosed" in {data.litigation.status, data.regulatory.status}
    ):
        unknown = data.litigation if data.litigation.status == "not_disclosed" else data.regulatory
        evidence = unknown.coverage_evidence
        assert evidence is not None
        try:
            document = _matter_document_for(evidence, matter_documents)
            values.append(SourceValue(
                field_id="litigation_regulatory_details", value="Not disclosed", raw_value=[],
                source=document.source, source_url=str(evidence.source_url), captured_at=captured_at,
                period=evidence.disclosure_date.isoformat(),
            ))
        except Exception as exc:
            errors.append(Part11Error(field_id="litigation_regulatory_details", reason=str(exc)))
    else:
        errors.append(Part11Error(field_id="litigation_regulatory_details", reason="No verified in-scope matter or explicit negative disclosures."))

    expected_company_name = company_name.strip()
    accepted_news = []
    for article in data.news_articles:
        if article.company_name.strip() != expected_company_name:
            errors.append(Part11Error(
                field_id="negative_news_input",
                reason=(
                    "News article company_name does not exactly match the target company: "
                    f"{article.company_name!r} != {company_name!r} ({article.original_url})"
                ),
            ))
            continue
        accepted_news.append(article)
    news_events: list[NewsEvent] = select_and_deduplicate_news(accepted_news, as_of=as_of)
    if not news_events:
        errors.extend([
            Part11Error(field_id="negative_news_summary", reason="No verified in-scope negative news article."),
            Part11Error(field_id="negative_news_attachment", reason="No verified in-scope negative news article."),
        ])
    else:
        summary_lines = []
        news_evidence = []
        for event_index, event in enumerate(news_events, start=1):
            primary = event.articles[0]
            summary = primary.english_summary or "Full text unavailable"
            summary_lines.append(
                f"{primary.published_date.isoformat()} | {primary.publisher} | {primary.title} | {summary} | {primary.original_url}"
            )
            for article in event.articles:
                news_evidence.append(ItemEvidence(
                    item_id=f"{event_index}:{article.publisher}", source="news",
                    source_url=str(article.original_url), period=article.published_date.isoformat(),
                    raw_value=article.model_dump(mode="json"),
                ))
        primary = news_events[0].articles[0]
        values.append(SourceValue(
            field_id="negative_news_summary", value="\n".join(summary_lines),
            raw_value=[item.model_dump(mode="json") for item in news_events], source="news",
            source_url=str(primary.original_url), captured_at=captured_at,
            period="rolling 12 months", item_evidence=news_evidence,
        ))
        attachment = build_negative_news_attachment(
            news_events, output_path=artifact_dir / "negative_news_articles.docx",
            company_name=company_name, as_of=as_of,
        )
        artifacts.append(Part11Artifact(path=str(attachment.resolve())))

    return Part11Result(
        status="completed" if not errors else "partial" if values else "failed",
        source_values=values,
        raw_result={
            "audit": data.latest_audit.model_dump(mode="json"),
            "news_events": [item.model_dump(mode="json") for item in news_events],
        },
        artifacts=artifacts,
        errors=errors,
    )


def collect_part11_groups(
    *,
    documents: list[EvidenceDocument],
    matter_documents: list[MatterEvidenceDocument],
    extracted: dict[str, Any],
    groups: set[str],
    company_name: str,
    as_of: date,
    captured_at: datetime,
    artifact_dir: Path,
) -> Part11Result:
    """Collect only the extraction groups that completed successfully.

    This deliberately does not synthesize placeholder disclosures for failed
    groups.  A failed extraction can therefore only produce its group error at
    the integration boundary, while verified values from other groups remain
    eligible for writing.
    """
    documents = [
        EvidenceDocument.model_validate(item) if isinstance(item, dict) else item
        for item in documents
    ]
    matter_documents = [
        MatterEvidenceDocument.model_validate(item) if isinstance(item, dict) else item
        for item in matter_documents
    ]
    values: list[SourceValue] = []
    errors: list[Part11Error] = []
    artifacts: list[Part11Artifact] = []

    def attempt(field_id: str, builder: Any) -> None:
        try:
            values.append(builder())
        except Exception as exc:
            errors.append(Part11Error(field_id=field_id, reason=str(exc)))

    if "audit" in groups:
        audit = extracted["audit"]
        latest = audit.latest_audit
        big4 = identify_big4(latest.auditor_name)
        if big4 is None and not latest.non_big4_background:
            errors.append(Part11Error(field_id="auditor_profile", reason="Non-Big 4 auditor background was not disclosed."))
        else:
            profile = (
                f"{latest.auditor_name} | Big 4: Yes ({big4})"
                if big4 else
                f"{latest.auditor_name} | Big 4: No | Background: {latest.non_big4_background}"
            )
            attempt("auditor_profile", lambda: _value(
                "auditor_profile", profile, latest.evidence, documents, captured_at,
                latest.model_dump(mode="json")
            ))
        attempt("audit_opinion", lambda: _value(
            "audit_opinion", latest.opinion, latest.evidence, documents, captured_at,
            latest.model_dump(mode="json")
        ))
        attempt("qualified_opinion_details", lambda: _value(
            "qualified_opinion_details",
            latest.modified_opinion_details or "N/A - standard unqualified opinion",
            latest.evidence, documents, captured_at, latest.model_dump(mode="json")
        ))
        changes = [
            item for item in audit.post_report_auditor_changes
            if latest.evidence.disclosure_date < item.effective_date <= as_of
        ]
        if audit.previous_audit is None:
            errors.extend([
                Part11Error(field_id="auditor_change_status", reason="Previous full-year audit record is unavailable."),
                Part11Error(field_id="auditor_change_details", reason="Previous full-year audit record is unavailable."),
            ])
        else:
            try:
                _document_for(audit.previous_audit.evidence, documents)
                changed = audit.previous_audit.auditor_name != latest.auditor_name or bool(changes)
                evidence = changes[0].evidence if changes else latest.evidence
                details = []
                if audit.previous_audit.auditor_name != latest.auditor_name:
                    details.append(
                        f"FY{audit.previous_audit.fiscal_year} {audit.previous_audit.auditor_name} -> "
                        f"FY{latest.fiscal_year} {latest.auditor_name}"
                    )
                details.extend(
                    f"{item.effective_date.isoformat()} | {item.former_auditor} -> {item.new_auditor} | {item.reason}"
                    for item in changes
                )
                attempt("auditor_change_status", lambda: _value(
                    "auditor_change_status", "Yes" if changed else "No", evidence,
                    documents, captured_at, {"events": [item.model_dump(mode="json") for item in changes]}
                ))
                attempt("auditor_change_details", lambda: _value(
                    "auditor_change_details", "\n".join(details) if details else "Not applicable",
                    evidence, documents, captured_at,
                    {"events": [item.model_dump(mode="json") for item in changes]},
                    items=_fact_items(changes, documents)
                ))
            except Exception as exc:
                errors.extend([
                    Part11Error(field_id="auditor_change_status", reason=str(exc)),
                    Part11Error(field_id="auditor_change_details", reason=str(exc)),
                ])

    if "restatements" in groups:
        restatement = extracted["restatements"].restatements
        if restatement.status == "not_disclosed":
            evidence = restatement.coverage_evidence
            assert evidence is not None
            attempt("financial_restatement_status", lambda: _value(
                "financial_restatement_status", "Not disclosed", evidence, documents,
                captured_at, restatement.model_dump(mode="json")
            ))
            attempt("financial_restatement_details", lambda: _value(
                "financial_restatement_details", "Not disclosed", evidence, documents,
                captured_at, restatement.model_dump(mode="json")
            ))
        else:
            window = subtract_years(as_of, 2)
            events = [item for item in restatement.events if window <= item.event_date <= as_of]
            if restatement.status == "yes" and not events:
                errors.extend([
                    Part11Error(field_id="financial_restatement_status", reason="No disclosed restatement falls in the 24-month window."),
                    Part11Error(field_id="financial_restatement_details", reason="No disclosed restatement falls in the 24-month window."),
                ])
            else:
                evidence = events[0].evidence if events else restatement.negative_evidence
                assert evidence is not None
                detail = "\n".join(
                    f"{item.event_date.isoformat()} | {item.periods_affected} | {item.details}" for item in events
                ) or "Not applicable"
                attempt("financial_restatement_status", lambda: _value(
                    "financial_restatement_status", "Yes" if events else "No", evidence,
                    documents, captured_at, restatement.model_dump(mode="json")
                ))
                attempt("financial_restatement_details", lambda: _value(
                    "financial_restatement_details", detail, evidence, documents, captured_at,
                    restatement.model_dump(mode="json"), items=_fact_items(events, documents)
                ))

    material_groups = {
        "board_changes": ("board_officer_material_change", "board_officer", "board_officer_changes"),
        "business_changes": ("business_operation_material_change", "business_operation", "business_operation_changes"),
        "shareholder_changes": ("top3_shareholder_change", "top3_shareholder", "top3_shareholder_changes"),
    }
    for group, (field_id, category, attribute) in material_groups.items():
        if group not in groups:
            continue
        disclosure = getattr(extracted[group], attribute)
        if any(item.category != category for item in disclosure.events):
            errors.append(Part11Error(field_id=field_id, reason=f"{category} disclosure contains another category."))
            continue
        events = [item for item in disclosure.events if item.event_date <= as_of]
        if disclosure.status == "not_disclosed":
            assert disclosure.coverage_evidence is not None
            attempt(field_id, lambda: _value(
                field_id, "Not disclosed", disclosure.coverage_evidence, documents,
                captured_at, disclosure.model_dump(mode="json")
            ))
            continue
        evidence = events[0].evidence if events else disclosure.negative_evidence
        if evidence is None:
            errors.append(Part11Error(field_id=field_id, reason="Material change evidence is missing."))
            continue
        attempt(field_id, lambda: _value(
            field_id, "Yes" if events else "No", evidence, documents, captured_at,
            disclosure.model_dump(mode="json"), items=_fact_items(events, documents)
        ))

    legal_groups = {
        "litigation": ("litigation_status", "litigation"),
        "regulatory": ("regulatory_status", "regulatory"),
    }
    selected_by_kind: dict[str, list[LegalRegulatoryMatter]] = {}
    for group, (field_id, kind) in legal_groups.items():
        if group not in groups:
            continue
        disclosure = getattr(extracted[group], kind)
        if disclosure.status == "not_disclosed":
            evidence = disclosure.coverage_evidence
            assert evidence is not None
            try:
                document = _matter_document_for(evidence, documents + matter_documents)
                values.append(SourceValue(
                    field_id=field_id, value="Not disclosed", raw_value=disclosure.model_dump(mode="json"),
                    source=document.source, source_url=str(evidence.source_url), captured_at=captured_at,
                    period=evidence.disclosure_date.isoformat(),
                ))
            except Exception as exc:
                errors.append(Part11Error(field_id=field_id, reason=str(exc)))
            selected_by_kind[kind] = []
            continue
        try:
            selected = select_in_scope_matters(disclosure, kind=kind, as_of=as_of)
            selected_by_kind[kind] = selected
            evidence = selected[0].evidence if selected else disclosure.negative_evidence
            assert evidence is not None
            document = _matter_document_for(evidence, documents + matter_documents)
            values.append(SourceValue(
                field_id=field_id, value="Yes" if selected else "No",
                raw_value=disclosure.model_dump(mode="json"), source=document.source,
                source_url=str(evidence.source_url), captured_at=captured_at,
                period=evidence.disclosure_date.isoformat(), item_evidence=_matter_items(selected, documents + matter_documents),
            ))
        except Exception as exc:
            errors.append(Part11Error(field_id=field_id, reason=str(exc)))
            selected_by_kind[kind] = []

    if "news" in groups:
        news_articles = extracted["news"].news_articles
        accepted = []
        for article in news_articles:
            if article.company_name.strip() != company_name.strip():
                errors.append(Part11Error(
                    field_id="negative_news_input",
                    reason=f"News article company_name does not exactly match the target company: {article.company_name!r} != {company_name!r} ({article.original_url})",
                ))
                continue
            accepted.append(article)
        news_events = select_and_deduplicate_news(accepted, as_of=as_of)
        if not news_events:
            errors.extend([
                Part11Error(field_id="negative_news_summary", reason="No verified in-scope negative news article."),
                Part11Error(field_id="negative_news_attachment", reason="No verified in-scope negative news article."),
            ])
        else:
            summary_lines = []
            news_evidence = []
            for event_index, event in enumerate(news_events, start=1):
                primary = event.articles[0]
                summary_lines.append(
                    f"{primary.published_date.isoformat()} | {primary.publisher} | {primary.title} | "
                    f"{primary.english_summary or 'Full text unavailable'} | {primary.original_url}"
                )
                news_evidence.extend(ItemEvidence(
                    item_id=f"{event_index}:{article.publisher}", source="news",
                    source_url=str(article.original_url), period=article.published_date.isoformat(),
                    raw_value=article.model_dump(mode="json")
                ) for article in event.articles)
            primary = news_events[0].articles[0]
            values.append(SourceValue(
                field_id="negative_news_summary", value="\n".join(summary_lines),
                raw_value=[item.model_dump(mode="json") for item in news_events], source="news",
                source_url=str(primary.original_url), captured_at=captured_at,
                period="rolling 12 months", item_evidence=news_evidence,
            ))
            try:
                attachment = build_negative_news_attachment(
                    news_events, output_path=artifact_dir / "negative_news_articles.docx",
                    company_name=company_name, as_of=as_of,
                )
                artifacts.append(Part11Artifact(path=str(attachment.resolve())))
            except Exception as exc:
                errors.append(Part11Error(field_id="negative_news_attachment", reason=str(exc)))

    return Part11Result(
        status="completed" if not errors else "partial" if values else "failed",
        source_values=values,
        raw_result={group: extracted[group].model_dump(mode="json") for group in groups if group in extracted},
        artifacts=artifacts,
        errors=errors,
    )


def part11_node(state: dict[str, Any], *, as_of: date) -> dict[str, Any]:
    raw = state.get("part_results", {}).get("part_11_input")
    if not isinstance(raw, dict):
        raise ValueError("part_results.part_11_input is required")
    result = collect_part11(
        Part11Input.model_validate(raw), company_name=state["company"]["company_name"],
        as_of=as_of, captured_at=datetime.fromisoformat(state["created_at"]),
        artifact_dir=Path(state["run_dir"]),
    )
    return {
        "part_results": {**state.get("part_results", {}), "part_11": result.model_dump(mode="json")},
        "source_values": [*state.get("source_values", []), *(item.model_dump(mode="json") for item in result.source_values)],
        "artifacts": [*state.get("artifacts", []), *(item.model_dump(mode="json") for item in result.artifacts)],
        "node_errors": [*state.get("node_errors", []), *({"node": "part_11", "field_id": item.field_id, "message": item.reason} for item in result.errors)],
    }
