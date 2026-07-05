from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


EXPLICIT_NEGATIVE_MARKERS = (
    "不存在", "未发生", "未出现", "没有", "无重大", "不涉及", "不适用",
    "none", "no material", "did not occur", "not applicable",
)


def has_explicit_negative_statement(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    return any(marker in normalized for marker in EXPLICIT_NEGATIVE_MARKERS)


class MatterEvidence(BaseModel):
    source: Literal["annual_report", "interim_report", "cninfo", "exchange"]
    source_url: HttpUrl
    disclosure_date: date
    evidence_text: str = Field(min_length=1)
    page_number: int = Field(ge=1)


class LegalRegulatoryMatter(BaseModel):
    kind: Literal["litigation", "regulatory"]
    event_date: date
    subject: str = Field(min_length=1)
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    status: Literal["pending", "closed", "investigation", "penalty"]
    authority: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    materiality_basis: Literal["official_material", "official_major", "official_important"]
    pending_in_latest_report: bool = False
    evidence: MatterEvidence

    @model_validator(mode="after")
    def validate_amount(self) -> "LegalRegulatoryMatter":
        if (self.amount is None) != (self.currency is None):
            raise ValueError("matter amount and currency must be supplied together")
        if self.pending_in_latest_report and self.status != "pending":
            raise ValueError("pending_in_latest_report requires status=pending")
        return self


class MatterDisclosure(BaseModel):
    status: Literal["yes", "no", "not_disclosed"]
    matters: list[LegalRegulatoryMatter] = Field(default_factory=list)
    negative_evidence: MatterEvidence | None = None
    coverage_evidence: MatterEvidence | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "MatterDisclosure":
        if self.status == "yes" and not self.matters:
            raise ValueError("yes matter disclosure requires at least one matter")
        if self.status == "no" and (self.matters or self.negative_evidence is None):
            raise ValueError("No requires an explicit formal negative disclosure")
        if (
            self.status == "no"
            and self.negative_evidence is not None
            and not has_explicit_negative_statement(self.negative_evidence.evidence_text)
        ):
            raise ValueError("No requires an explicitly negative quotation")
        if self.status == "not_disclosed" and (
            self.matters or self.negative_evidence is not None or self.coverage_evidence is None
        ):
            raise ValueError("not_disclosed requires coverage evidence and no matters")
        return self


def subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def select_in_scope_matters(
    disclosure: MatterDisclosure,
    *,
    kind: Literal["litigation", "regulatory"],
    as_of: date,
) -> list[LegalRegulatoryMatter]:
    window_start = subtract_years(as_of, 2)
    selected = []
    for matter in disclosure.matters:
        if matter.kind != kind:
            raise ValueError(f"{kind} disclosure contains a {matter.kind} matter")
        if matter.event_date > as_of or matter.evidence.disclosure_date > as_of:
            raise ValueError("matter date cannot be after the run date")
        if matter.pending_in_latest_report or window_start <= matter.event_date <= as_of:
            selected.append(matter)
    return sorted(selected, key=lambda item: item.event_date, reverse=True)


def format_matters(matters: list[LegalRegulatoryMatter]) -> str:
    lines = []
    for item in matters:
        amount = "Not disclosed" if item.amount is None else f"{item.currency} {item.amount}"
        lines.append(
            f"{item.event_date.isoformat()} | {item.subject} | Amount: {amount} | "
            f"Status: {item.status} | Authority: {item.authority} | {item.summary}"
        )
    return "\n".join(lines)
