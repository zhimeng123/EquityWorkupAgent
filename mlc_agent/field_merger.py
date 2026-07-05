from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from mlc_agent.company_resolver import build_eastmoney_url, build_xueqiu_url
from mlc_agent.schemas import (
    CompanyIdentity,
    ConflictRecord,
    EvidenceRecord,
    FailedField,
    FieldResult,
    SourceValue,
)


def build_static_values(
    company: CompanyIdentity,
    template_path: Path,
    captured_at: datetime,
) -> list[SourceValue]:
    eastmoney_url = build_eastmoney_url(company)
    xueqiu_url = build_xueqiu_url(company)
    template_uri = template_path.resolve().as_uri()
    return [
        SourceValue(
            field_id="country_of_incorporation",
            value="PRC",
            raw_value="PRC",
            source="fixed",
            source_url=template_uri,
            captured_at=captured_at,
        ),
        SourceValue(
            field_id="listing_status",
            value="Listed",
            raw_value="Listed",
            source="fixed",
            source_url=template_uri,
            captured_at=captured_at,
        ),
        SourceValue(
            field_id="eastmoney_url",
            value=eastmoney_url,
            raw_value=eastmoney_url,
            source="system",
            source_url=eastmoney_url,
            captured_at=captured_at,
        ),
        SourceValue(
            field_id="xueqiu_url",
            value=xueqiu_url,
            raw_value=xueqiu_url,
            source="system",
            source_url=xueqiu_url,
            captured_at=captured_at,
        ),
    ]


def merge_field_values(
    mappings: list[dict[str, Any]],
    priorities: dict[str, list[str]],
    candidates: list[SourceValue],
) -> tuple[dict[str, FieldResult], list[EvidenceRecord], list[ConflictRecord], list[FailedField]]:
    grouped: dict[str, list[SourceValue]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.field_id, []).append(candidate)

    results: dict[str, FieldResult] = {}
    evidence: list[EvidenceRecord] = []
    conflicts: list[ConflictRecord] = []
    failures: list[FailedField] = []

    for mapping in mappings:
        field_id = mapping["field_id"]
        label = mapping["label"]
        priority = priorities[field_id]
        field_candidates = [
            item for item in grouped.get(field_id, []) if item.source in priority
        ]
        ordered = sorted(
            field_candidates,
            key=lambda item: priority.index(item.source) if item.source in priority else len(priority),
        )
        if not ordered:
            failures.append(
                FailedField(
                    field_id=field_id,
                    label=label,
                    reason="No verified value was returned by the configured sources.",
                    source_attempted=priority,
                )
            )
            continue

        selected = ordered[0]
        distinct = {item.value.strip() for item in ordered}
        conflict_values = [
            {"source": item.source, "value": item.value, "source_url": item.source_url}
            for item in ordered[1:]
            if item.value.strip() != selected.value.strip()
        ]
        reason = f"Selected {selected.source} by configured priority: {' > '.join(priority)}."
        result = FieldResult(
            field_id=field_id,
            label=label,
            value=selected.value,
            selected_source=selected.source,
            period=selected.period,
            structured_value=selected.metadata.get("structured_value"),
            artifact_path=selected.metadata.get("artifact_path"),
        )
        results[field_id] = result
        evidence.append(
            EvidenceRecord(
                field_id=field_id,
                value=selected.value,
                selected_source=selected.source,
                source_url=selected.source_url,
                captured_at=selected.captured_at,
                raw_value=selected.raw_value,
                normalized_value=selected.value,
                period=selected.period,
                selection_reason=reason,
                conflict_values=conflict_values,
                supporting_sources=selected.metadata.get("supporting_sources", []),
                derived_evidence=selected.derived_evidence,
                item_evidence=selected.item_evidence,
            )
        )
        if len(distinct) > 1:
            conflicts.append(
                ConflictRecord(
                    field_id=field_id,
                    candidates=[
                        {"source": item.source, "value": item.value, "source_url": item.source_url}
                        for item in ordered
                    ],
                    selected_source=selected.source,
                    selection_reason=reason,
                )
            )

    return results, evidence, conflicts, failures
