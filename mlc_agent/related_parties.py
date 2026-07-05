from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from mlc_agent.schemas import ItemEvidence, SourceValue


DisclosureStatus = bool | Literal["Not disclosed"]


class RelatedPartyTransaction(BaseModel):
    related_party: str = Field(min_length=1)
    relationship: str = Field(min_length=1)
    transaction_type: str = Field(min_length=1)
    amount_cny: Decimal | None = None
    commercial_terms: str | Literal["Not disclosed"] = "Not disclosed"
    board_approved: DisclosureStatus = "Not disclosed"
    period: str = Field(min_length=1)
    source: Literal["annual_report", "interim_report", "cninfo", "exchange"]
    source_url: str
    source_page: int | None = Field(default=None, ge=1)


def _approval(value: DisclosureStatus) -> str:
    if value == "Not disclosed":
        return value
    return "Yes" if value else "No"


def collect_related_party_transactions(
    transactions: list[RelatedPartyTransaction],
    *,
    captured_at: datetime,
) -> SourceValue | None:
    if not transactions:
        return None
    lines: list[str] = []
    evidence: list[ItemEvidence] = []
    for index, item in enumerate(transactions, start=1):
        amount = (
            "Not disclosed"
            if item.amount_cny is None
            else f"CNY {item.amount_cny / Decimal('1000000'):,.2f} million"
        )
        lines.append(
            f"{index}. {item.related_party} ({item.relationship}) - {item.transaction_type}; "
            f"Amount: {amount}; Terms: {item.commercial_terms}; "
            f"Board approval: {_approval(item.board_approved)}."
        )
        evidence.append(
            ItemEvidence(
                item_id=str(index),
                source=item.source,
                source_url=item.source_url,
                period=item.period,
                raw_value=item.model_dump(mode="json"),
            )
        )
    first = transactions[0]
    return SourceValue(
        field_id="related_party_transactions",
        value="\n".join(lines),
        raw_value=[item.model_dump(mode="json") for item in transactions],
        source=first.source,
        source_url=first.source_url,
        captured_at=captured_at,
        period="; ".join(dict.fromkeys(item.period for item in transactions)),
        item_evidence=evidence,
    )
