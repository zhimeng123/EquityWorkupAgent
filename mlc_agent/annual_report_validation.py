from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from openai import OpenAI
from dotenv import load_dotenv

from mlc_agent.company_supplement import (
    DisclosureSection,
    EvidenceDocument as Part01Document,
    ListedSubsidiary,
    collect_company_supplement,
)
from mlc_agent.cninfo import AnnouncementDocument
from mlc_agent.llm import BASE_URL, MODEL
from mlc_agent.operating_performance import ReportDocument, extract_revenue_breakdown
from mlc_agent.production_adapters import (
    ParsedDisclosure,
    Part11AuditExtraction,
    Part11BoardChangesExtraction,
    Part11LitigationExtraction,
    Part11RegulatoryExtraction,
    Part11RestatementExtraction,
    SharedDisclosureBundle,
    evidence_text_documents,
    extract_part05_related_party_transactions,
    extract_pydantic,
    filtered_disclosure_payload,
)
from mlc_agent.related_parties import collect_related_party_transactions
from mlc_agent.report_parser import ParsedReport, parse_machine_generated_pdf
from mlc_agent.us_exposure import (
    EvidenceDocument as Part03Document,
    collect_us_exposure,
    make_openai_extractor as make_part03_extractor,
)
from mlc_agent.annual_report_rules import (
    extract_annual_audit,
    extract_annual_litigation,
    extract_annual_regulatory,
    extract_annual_restatement,
)


ANNUAL_FIELD_IDS = {
    "listed_subsidiaries",
    "us_subsidiary_status", "us_subsidiary_count", "us_subsidiary_details",
    "us_exposure_other_details", "us_revenue", "us_revenue_ratio",
    "us_employee_count", "us_employee_by_state", "revenue_breakdown",
    "related_party_transactions",
}
PART11_GROUPS = {
    "audit": Part11AuditExtraction,
    "restatements": Part11RestatementExtraction,
    "board_changes": Part11BoardChangesExtraction,
    "litigation": Part11LitigationExtraction,
    "regulatory": Part11RegulatoryExtraction,
}


def build_local_annual_bundle(
    parsed: ParsedReport,
    *,
    stock_code: str,
    report_year: int,
    published_at: datetime,
) -> SharedDisclosureBundle:
    """Build the normal disclosure contract without catalog or network access."""
    source_url = f"https://local.invalid/{Path(parsed.path).name}"
    document = AnnouncementDocument(
        announcement_id=Path(parsed.path).stem,
        stock_code=stock_code,
        title=f"{report_year}年年度报告（本地验证）",
        published_at=published_at,
        url=source_url,
        document_type="annual_report",
        report_year=report_year,
    )
    return SharedDisclosureBundle(
        org_id="local-offline-validation",
        documents=[ParsedDisclosure(document=document, parsed=parsed)],
        announcement_catalog=[],
    )


def _listed_extractor(client: Any, *, model: str):
    def extract(documents: list[Part01Document], *, as_of: date) -> dict[str, Any]:
        output_model = DisclosureSection[ListedSubsidiary]
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "Extract listed subsidiaries only. A controlled/major subsidiary table does not prove "
                    "separate listing. Return yes only when name, stock code, exchange and listing status are "
                    "explicitly stated. Otherwise return not_disclosed with coverage evidence from the reviewed "
                    "subsidiary section. Never infer exchange listing attributes. Every evidence_text must be "
                    "an exact contiguous Chinese quotation. Return only one JSON object matching this schema: "
                    + json.dumps(output_model.model_json_schema(), ensure_ascii=False)
                )},
                {"role": "user", "content": json.dumps({
                    "as_of": as_of.isoformat(),
                    "documents": [item.model_dump(mode="json") for item in documents],
                }, ensure_ascii=False)},
            ],
            temperature=0,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM returned empty listed-subsidiary extraction")
        raw = json.loads(content)
        return {"listed_subsidiaries": raw}

    return extract


def _source_values(items: list[Any], allowed: set[str]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items if item.field_id in allowed]


def _part11_payload(bundle: SharedDisclosureBundle, group: str) -> dict[str, Any]:
    patterns = {
        "audit": re.compile(r"审计报告|审计意见|会计师"),
        "restatements": re.compile(r"更正|重述|会计差错"),
        "board_changes": re.compile(r"董事|高级管理人员"),
        "litigation": re.compile(r"诉讼|仲裁"),
        "regulatory": re.compile(r"处罚|整改|监管"),
    }
    return {
        "documents": filtered_disclosure_payload(
            bundle,
            document_types={"annual_report"},
            page_pattern=patterns[group],
            adjacent_pages=1,
            latest_per_type=True,
        )
    }


def validate_parsed_annual_report(
    parsed: ParsedReport,
    *,
    client: Any,
    stock_code: str,
    report_year: int,
    published_at: datetime,
    as_of: date,
    model: str = MODEL,
) -> dict[str, Any]:
    """Validate only annual-report-backed MVP fields; performs no HTTP data retrieval."""
    bundle = build_local_annual_bundle(
        parsed,
        stock_code=stock_code,
        report_year=report_year,
        published_at=published_at,
    )
    captured_at = datetime.now(timezone.utc)
    errors: list[str] = []
    values: list[dict[str, Any]] = []

    part01_documents = [
        Part01Document.model_validate(item)
        for item in evidence_text_documents(
            bundle,
            document_types={"annual_report"},
            page_pattern=re.compile(r"上市子公司|控股子公司|主要控股参股公司"),
            adjacent_pages=2,
            latest_per_type=True,
        )
    ]
    part01 = collect_company_supplement(
        part01_documents,
        as_of=as_of,
        extractor=_listed_extractor(client, model=model),
        captured_at=captured_at,
    )
    values.extend(_source_values(part01.source_values, {"listed_subsidiaries"}))
    errors.extend(
        f"part01.{item.field_id}: {item.reason}"
        for item in part01.errors if item.field_id == "listed_subsidiaries"
    )

    part03_documents = [
        Part03Document.model_validate(item)
        for item in evidence_text_documents(
            bundle,
            document_types={"annual_report"},
            page_pattern=re.compile(r"美国|USA|United States|美洲|境外|子公司|分地区|员工|雇员"),
            adjacent_pages=1,
            latest_per_type=True,
        )
    ]
    part03 = collect_us_exposure(
        part03_documents,
        as_of=as_of,
        extractor=make_part03_extractor(client, model=model),
        captured_at=captured_at,
    )
    values.extend(_source_values(part03.source_values, ANNUAL_FIELD_IDS))
    errors.extend(f"part03.{item.field_id}: {item.reason}" for item in part03.errors)

    report = ReportDocument(
        report=parsed,
        source_url=bundle.documents[0].document.url,
        report_year=report_year,
        kind="annual_report",
    )
    breakdown = extract_revenue_breakdown([report])
    if breakdown:
        values.append({
            "field_id": "revenue_breakdown",
            "value": [item.model_dump(mode="json") for item in breakdown],
            "source": "annual_report",
            "source_url": bundle.documents[0].document.url,
            "period": f"FY{report_year}",
        })
    else:
        errors.append("part04.revenue_breakdown: no verified business/geographic revenue table")

    try:
        related_transactions = extract_part05_related_party_transactions(
            client,
            model=model,
            bundle=bundle,
        )
        related_value = collect_related_party_transactions(
            related_transactions,
            captured_at=captured_at,
        )
        if related_value is None:
            errors.append("part05.related_party_transactions: no verified transaction disclosure")
        else:
            values.append(related_value.model_dump(mode="json"))
    except Exception as exc:
        errors.append(f"part05.related_party_transactions: {exc}")

    part11: dict[str, Any] = {}
    prompts = {
        "audit": "Extract auditor and audit opinion from this annual report only.",
        "restatements": "Extract financial restatements or explicit no-restatement disclosure only.",
        "board_changes": "Extract disclosed director changes with person, role, date and reason only.",
        "litigation": "Distinguish no major litigation from other disclosed litigation and extract the latter.",
        "regulatory": "Extract penalties and rectification, or an explicit no/not-applicable disclosure only.",
    }
    for group, output_model in PART11_GROUPS.items():
        try:
            deterministic = {
                "audit": extract_annual_audit,
                "restatements": extract_annual_restatement,
                "litigation": extract_annual_litigation,
                "regulatory": extract_annual_regulatory,
            }.get(group)
            if deterministic is not None:
                extracted = deterministic(
                    parsed,
                    source_url=str(bundle.documents[0].document.url),
                    published_on=published_at.date(),
                    report_year=report_year,
                )
            else:
                extracted = extract_pydantic(
                    client,
                    model=model,
                    output_model=output_model,
                    system_prompt=(
                        prompts[group]
                        + " Use exact Chinese quotations. Use no only for an explicit negative quotation; use "
                        "not_disclosed only with coverage evidence from the supplied formal topic section."
                    ),
                    payload={"as_of": as_of, **_part11_payload(bundle, group)},
                )
            part11[group] = extracted.model_dump(mode="json")
        except Exception as exc:
            errors.append(f"part11.{group}: {exc}")

    return {
        "input_pdf": parsed.path,
        "network_scope": "DeepSeek API only; no source websites accessed",
        "report_year": report_year,
        "source_values": values,
        "part11_extractions": part11,
        "errors": errors,
    }


def run_annual_report_validation(
    pdf_path: Path,
    output_dir: Path,
    *,
    stock_code: str,
    report_year: int,
    published_at: datetime,
    as_of: date,
) -> Path:
    pdf_path = pdf_path.resolve()
    if not pdf_path.is_file():
        raise ValueError(f"local annual report does not exist: {pdf_path}")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is not set")
    parsed = parse_machine_generated_pdf(pdf_path)
    client = OpenAI(api_key=api_key, base_url=BASE_URL, timeout=60.0)
    result = validate_parsed_annual_report(
        parsed,
        client=client,
        stock_code=stock_code,
        report_year=report_year,
        published_at=published_at,
        as_of=as_of,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "annual_report_fields.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return output_path
