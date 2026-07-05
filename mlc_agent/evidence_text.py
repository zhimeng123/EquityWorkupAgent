from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any


def normalize_evidence_text(value: str) -> str:
    """Normalize Unicode and whitespace only; preserve every non-whitespace character."""
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def contains_normalized_evidence(document_text: str, evidence_text: str) -> bool:
    return normalize_evidence_text(evidence_text) in normalize_evidence_text(document_text)


def _page_texts(document: dict[str, Any]) -> dict[int, str]:
    if isinstance(document.get("pages"), list):
        return {int(p["page_number"]): str(p.get("text") or "") for p in document["pages"] if p.get("page_number") is not None}
    text = str(document.get("text") or "")
    matches = list(re.finditer(r"\[Page (\d+)\]\s*", text))
    if not matches:
        return {1: text}
    return {int(m.group(1)): text[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)] for i, m in enumerate(matches)}


def _contiguous_candidates(page_text: str) -> list[str]:
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    candidates = list(lines)
    for line in lines:
        candidates.extend(item.strip() for item in re.split(r"(?<=[。！？；.!?;])", line) if item.strip())
    return list(dict.fromkeys(candidates))


def anchor_extracted_evidence(output: Any, documents: list[dict[str, Any]]) -> Any:
    """Store a deterministic excerpt and its actual supplied source page.

    The model-provided page is only a search hint.  Page identity is established
    locally from the supplied page text, so a hallucinated page number can never
    be persisted as evidence.
    """
    anchored = deepcopy(output)
    by_url = {str(doc.get("source_url")): _page_texts(doc) for doc in documents if doc.get("source_url")}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "evidence_text" in value and value.get("source_url"):
                url = str(value["source_url"])
                pages = by_url.get(url)
                if not pages:
                    raise ValueError(f"evidence URL was not supplied: {url}")
                page_number = value.get("page_number")
                raw_hint = str(value.get("evidence_text") or "")
                hint = normalize_evidence_text(raw_hint)
                matches: list[tuple[int, int, int, int, str]] = []
                for actual_page, page_text in pages.items():
                    candidates = _contiguous_candidates(page_text)
                    for candidate in candidates:
                        normalized = normalize_evidence_text(candidate)
                        if not hint or hint not in normalized:
                            continue
                        matches.append((
                            int(hint == normalized),
                            int(page_number is not None and actual_page == int(page_number)),
                            -len(candidate),
                            -actual_page,
                            candidate,
                        ))
                if not matches:
                    raise ValueError(f"evidence fact could not be anchored in supplied pages: {url}")
                best = max(matches)
                selected = best[-1]
                selected_page = -best[-2]
                value["page_number"] = selected_page
                value["evidence_text"] = selected
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(anchored)
    return anchored
