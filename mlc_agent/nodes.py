from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docx import Document

from mlc_agent.company_resolver import (
    build_eastmoney_url,
    resolve_a_share_company,
)
from mlc_agent.config import load_source_priorities, load_template_mapping
from mlc_agent.docx_writer import (
    find_unmarked_non_mvp_placeholders,
    validate_template_mapping,
    write_docx_by_mapping,
)
from mlc_agent.eastmoney import (
    fetch_company_profile,
    fetch_financial_period_records,
    fetch_financial_summary,
    fetch_market_snapshot,
    normalize_eastmoney_values,
)
from mlc_agent.exceptions import InputValidationError, WorkupAgentError
from mlc_agent.field_merger import build_static_values, merge_field_values
from mlc_agent.http_client import build_http_client
from mlc_agent.json_io import save_json
from mlc_agent.llm import API_KEY_ENV, get_llm_api_key, summarize_business_description
from mlc_agent.logging_utils import configure_run_logger, get_logger
from mlc_agent.official_site import fetch_official_profile
from mlc_agent.schemas import (
    CompanyIdentity,
    ExecutionStep,
    FailedField,
    FieldResult,
    SelfCheckResult,
    SourceValue,
    WorkupAgentState,
)
from mlc_agent.xueqiu import fetch_xueqiu_snapshot, normalize_xueqiu_values


PLAN_DEFINITIONS = [
    ("validate_template", "Validate the fixed DOCX template and mapping"),
    ("resolve_company", "Resolve one unambiguous A-share company"),
    ("fetch_eastmoney", "Collect Eastmoney profile, financial and quote data"),
    ("fetch_xueqiu", "Collect Xueqiu market data without login state"),
    ("fetch_official_site", "Collect and summarize the official company profile"),
    ("shared_disclosures", "Discover, download and parse company disclosures once"),
    ("part_01", "Collect company supplemental information"),
    ("part_02", "Validate company-specific external links"),
    ("part_03", "Collect United States exposure"),
    ("part_04", "Collect operating performance and standard financial periods"),
    ("part_05", "Select peers and collect related-party transactions"),
    ("part_06", "Calculate liquidity and debt metrics"),
    ("part_07", "Calculate deep financial analysis"),
    ("part_08", "Collect market history and securities analysis"),
    ("part_09", "Generate stock chart artifacts"),
    ("part_10", "Collect governance, shareholders and employees"),
    ("part_11", "Collect audit, changes, litigation and news"),
    ("merge_fields", "Apply configured source priorities and record conflicts"),
    ("write_docx", "Write verified values into the fixed DOCX positions"),
    ("generate_evidence", "Generate evidence, failure, raw data and plan files"),
    ("self_check", "Verify field, evidence and output integrity"),
    ("finalize", "Report final output paths and counts"),
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _append_error(state: WorkupAgentState, node: str, message: str) -> list[dict[str, str]]:
    errors = list(state.get("errors", []))
    errors.append({"node": node, "message": message, "captured_at": _utc_now().isoformat()})
    get_logger().warning("%s: %s", node, message)
    return errors


def _mark_step(
    state: WorkupAgentState,
    step_id: str,
    status: str,
    detail: str | None = None,
) -> list[dict[str, Any]]:
    steps = [dict(step) for step in state.get("execution_plan", [])]
    for step in steps:
        if step["step_id"] == step_id:
            step["status"] = status
            step["detail"] = detail
            break
    return steps


def initialize_run_node(state: WorkupAgentState) -> dict[str, Any]:
    template_path = Path(state["template_path"]).expanduser().resolve()
    if not template_path.exists() or not template_path.is_file():
        raise InputValidationError(f"模板文件不存在: {template_path}")
    if template_path.suffix.lower() != ".docx":
        raise InputValidationError("MVP1 只支持 .docx 模板。")

    output_root = Path(state["output_root"]).expanduser().resolve()
    company_slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", state["company_input"]).strip("_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_id = f"{timestamp}_{company_slug or 'company'}"
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    log_path = run_dir / "run.log"
    logger = configure_run_logger(log_path, state.get("debug", False))
    logger.info("Run initialized: %s", run_id)
    return {
        "run_id": run_id,
        "template_path": str(template_path),
        "output_root": str(output_root),
        "run_dir": str(run_dir),
        "created_at": _utc_now().isoformat(),
        "source_values": [],
        "errors": [],
        "field_results": {},
        "evidence_records": [],
        "conflict_records": [],
        "failed_fields": [],
        "step_results": [],
        "report_catalog": [],
        "announcement_catalog": [],
        "part_results": {},
        "artifacts": [],
        "node_errors": [],
        "log_path": str(log_path),
    }


def load_template_mapping_node(state: WorkupAgentState) -> dict[str, Any]:
    config = load_template_mapping(Path(state["config_dir"]))
    mappings = config["fields"]
    validate_template_mapping(Path(state["template_path"]), mappings)
    get_logger().info("Validated %d fixed template mappings", len(mappings))
    return {
        "template_version": config["template_version"],
        "field_mapping": mappings,
        "fillable_fields": [item["field_id"] for item in mappings],
    }


def create_execution_plan_node(state: WorkupAgentState) -> dict[str, Any]:
    plan = [ExecutionStep(step_id=step_id, title=title).model_dump(mode="json") for step_id, title in PLAN_DEFINITIONS]
    plan[0]["status"] = "completed"
    plan[0]["detail"] = f"Validated {len(state['field_mapping'])} fixed positions"
    get_logger().info("Created fixed execution plan with %d steps", len(plan))
    return {"execution_plan": plan, "current_step": "confirm_plan"}


def confirm_plan_node(state: WorkupAgentState) -> dict[str, Any]:
    if state.get("auto_confirm"):
        confirmed = True
    else:
        print("\nExecution plan:")
        for index, step in enumerate(state["execution_plan"], start=1):
            print(f"  {index}. {step['title']}")
        answer = input("Proceed? [y/N]: ").strip().lower()
        confirmed = answer in {"y", "yes"}
    if not confirmed:
        raise WorkupAgentError("用户拒绝执行计划，任务已终止。")
    get_logger().info("Execution plan confirmed")
    return {"confirmed": True}


def resolve_company_node(state: WorkupAgentState) -> dict[str, Any]:
    with build_http_client() as client:
        company = resolve_a_share_company(client, state["company_input"])
    get_logger().info("Resolved company %s (%s)", company.company_short_name, company.stock_code)
    return {
        "company": company.model_dump(mode="json"),
        "current_step": "resolve_company",
        "execution_plan": _mark_step(state, "resolve_company", "completed", company.stock_code),
    }


def fetch_eastmoney_node(state: WorkupAgentState) -> dict[str, Any]:
    company = CompanyIdentity.model_validate(state["company"])
    errors = list(state.get("errors", []))
    profile: dict[str, Any] | None = None
    financials: dict[str, Any] | None = None
    period_records: list[dict[str, Any]] = []
    market: dict[str, Any] | None = None
    with build_http_client() as client:
        try:
            profile = fetch_company_profile(client, company)
        except Exception as exc:
            errors = _append_error({**state, "errors": errors}, "fetch_eastmoney_profile", str(exc))
        try:
            financials = fetch_financial_summary(client, company)
        except Exception as exc:
            errors = _append_error({**state, "errors": errors}, "fetch_eastmoney_financials", str(exc))
        try:
            period_records = fetch_financial_period_records(client, company)
        except Exception as exc:
            errors = _append_error({**state, "errors": errors}, "fetch_eastmoney_periods", str(exc))
        try:
            total_shares = None
            if financials:
                total_shares = financials["latest"].get("TOTAL_SHARE")
            market = fetch_market_snapshot(client, company, total_shares)
        except Exception as exc:
            errors = _append_error({**state, "errors": errors}, "fetch_eastmoney_market", str(exc))

    if profile:
        company.company_name = str(profile.get("ORG_NAME") or company.company_name)
        company.company_short_name = str(profile.get("SECURITY_NAME_ABBR") or company.company_short_name)
        company.company_english_name = profile.get("ORG_NAME_EN")
        website = profile.get("ORG_WEB")
        if website:
            website = str(website).strip()
            company.official_website = website if website.startswith(("http://", "https://")) else f"https://{website}"

    captured_at = _utc_now()
    values = normalize_eastmoney_values(company, profile, financials, market, captured_at)
    get_logger().info("Eastmoney produced %d verified field candidates", len(values))
    all_values = list(state.get("source_values", [])) + [item.model_dump(mode="json") for item in values]
    return {
        "company": company.model_dump(mode="json"),
        "eastmoney_data": {"profile": profile or {}, "financials": financials or {}, "period_records": period_records, "market": market or {}},
        "source_values": all_values,
        "errors": errors,
        "current_step": "fetch_eastmoney",
        "execution_plan": _mark_step(state, "fetch_eastmoney", "completed", f"{len(values)} field candidates"),
    }


def fetch_xueqiu_node(state: WorkupAgentState) -> dict[str, Any]:
    company = CompanyIdentity.model_validate(state["company"])
    errors = list(state.get("errors", []))
    quote: dict[str, Any] = {}
    values: list[SourceValue] = []
    try:
        with build_http_client() as client:
            quote = fetch_xueqiu_snapshot(client, company)
        values = normalize_xueqiu_values(company, quote, _utc_now())
        detail = f"{len(values)} field candidates"
    except Exception as exc:
        errors = _append_error({**state, "errors": errors}, "fetch_xueqiu", str(exc))
        detail = "source unavailable; configured Eastmoney candidates retained"
    get_logger().info("Xueqiu step completed: %s", detail)
    return {
        "xueqiu_data": quote,
        "source_values": list(state.get("source_values", []))
        + [item.model_dump(mode="json") for item in values],
        "errors": errors,
        "current_step": "fetch_xueqiu",
        "execution_plan": _mark_step(state, "fetch_xueqiu", "completed", detail),
    }


def fetch_official_site_node(state: WorkupAgentState) -> dict[str, Any]:
    company = CompanyIdentity.model_validate(state["company"])
    profile = state.get("eastmoney_data", {}).get("profile", {})
    errors = list(state.get("errors", []))
    values: list[SourceValue] = []
    official_data: dict[str, Any] = {}
    description_source = "eastmoney"
    source_url = build_eastmoney_url(company)
    source_text = "\n".join(
        str(value).strip()
        for value in (profile.get("ORG_PROFILE"), profile.get("BUSINESS_SCOPE"))
        if value
    )

    if company.official_website:
        try:
            with build_http_client() as client:
                page = fetch_official_profile(client, company.official_website)
            official_data = {"profile_url": page.url, "profile_text": page.text}
            description_source = "official_site"
            source_url = page.url
            source_text = page.text
            values.append(
                SourceValue(
                    field_id="official_website",
                    value=company.official_website,
                    raw_value=company.official_website,
                    source="official_site",
                    source_url=page.url,
                    captured_at=_utc_now(),
                )
            )
        except Exception as exc:
            errors = _append_error({**state, "errors": errors}, "fetch_official_site", str(exc))

    if source_text:
        try:
            api_key = get_llm_api_key()
        except ValueError:
            errors = _append_error(
                {**state, "errors": errors}, "summarize_business_description", f"{API_KEY_ENV} is not set"
            )
        else:
            try:
                description = summarize_business_description(
                    api_key,
                    source_text,
                    company_name=company.company_english_name or company.company_name,
                    founded_date=profile.get("FOUND_DATE"),
                    listing_date=profile.get("LISTING_DATE"),
                    main_business=profile.get("MAIN_BUSINESS"),
                )
                supporting_sources = []
                if description_source == "official_site":
                    supporting_sources.append(
                        {"source": "eastmoney", "source_url": build_eastmoney_url(company)}
                    )
                values.append(
                    SourceValue(
                        field_id="business_description",
                        value=description,
                        raw_value=source_text,
                        source=description_source,
                        source_url=source_url,
                        captured_at=_utc_now(),
                        metadata={"supporting_sources": supporting_sources},
                    )
                )
            except Exception as exc:
                errors = _append_error({**state, "errors": errors}, "summarize_business_description", str(exc))
    else:
        errors = _append_error(
            {**state, "errors": errors}, "fetch_official_site", "No official or Eastmoney profile text available"
        )

    get_logger().info("Official-site step produced %d verified field candidates", len(values))

    return {
        "official_site_data": official_data,
        "source_values": list(state.get("source_values", []))
        + [item.model_dump(mode="json") for item in values],
        "errors": errors,
        "current_step": "fetch_official_site",
        "execution_plan": _mark_step(
            state, "fetch_official_site", "completed", f"{len(values)} field candidates"
        ),
    }


def merge_fields_node(state: WorkupAgentState) -> dict[str, Any]:
    company = CompanyIdentity.model_validate(state["company"])
    candidates = [SourceValue.model_validate(item) for item in state.get("source_values", [])]
    candidates.extend(build_static_values(company, Path(state["template_path"]), _utc_now()))
    priorities = load_source_priorities(Path(state["config_dir"]), state["field_mapping"])
    results, evidence, conflicts, failures = merge_field_values(
        state["field_mapping"], priorities, candidates
    )
    get_logger().info(
        "Merged fields: %d selected, %d failed, %d conflicts",
        len(results),
        len(failures),
        len(conflicts),
    )
    return {
        "field_results": {key: value.model_dump(mode="json") for key, value in results.items()},
        "evidence_records": [item.model_dump(mode="json") for item in evidence],
        "conflict_records": [item.model_dump(mode="json") for item in conflicts],
        "failed_fields": [item.model_dump(mode="json") for item in failures],
        "source_values": [item.model_dump(mode="json") for item in candidates],
        "current_step": "merge_fields",
        "execution_plan": _mark_step(state, "merge_fields", "completed", f"{len(results)} selected fields"),
    }


def write_docx_node(state: WorkupAgentState) -> dict[str, Any]:
    output_path = Path(state["run_dir"]) / "result.docx"
    results = {key: FieldResult.model_validate(value) for key, value in state["field_results"].items()}
    write_failures = write_docx_by_mapping(
        Path(state["template_path"]), output_path, state["field_mapping"], results
    )
    get_logger().info("DOCX written to %s with %d write failures", output_path, len(write_failures))
    failed_ids = {field_id for field_id, _ in write_failures}
    if failed_ids:
        mappings = {item["field_id"]: item for item in state["field_mapping"]}
        failed_fields = list(state["failed_fields"])
        for field_id, reason in write_failures:
            failed_fields.append(
                FailedField(
                    field_id=field_id,
                    label=mappings[field_id]["label"],
                    reason=f"DOCX write failed: {reason}",
                    source_attempted=[state["field_results"][field_id]["selected_source"]],
                ).model_dump(mode="json")
            )
        field_results = {
            key: value for key, value in state["field_results"].items() if key not in failed_ids
        }
        evidence = [item for item in state["evidence_records"] if item["field_id"] not in failed_ids]
    else:
        failed_fields = state["failed_fields"]
        field_results = state["field_results"]
        evidence = state["evidence_records"]
    return {
        "output_docx_path": str(output_path),
        "field_results": field_results,
        "evidence_records": evidence,
        "failed_fields": failed_fields,
        "current_step": "write_docx",
        "execution_plan": _mark_step(state, "write_docx", "completed", str(output_path)),
    }


def generate_evidence_files_node(state: WorkupAgentState) -> dict[str, Any]:
    run_dir = Path(state["run_dir"])
    sources_path = run_dir / "sources.json"
    failed_path = run_dir / "failed_fields.json"
    extracted_path = run_dir / "extracted_data.json"
    plan_path = run_dir / "execution_plan.json"
    save_json(sources_path, {"evidence": state["evidence_records"], "conflicts": state["conflict_records"]})
    save_json(failed_path, state["failed_fields"])
    save_json(
        extracted_path,
        {
            "run": {
                "run_id": state["run_id"],
                "goal": state["goal"],
                "created_at": state["created_at"],
                "template_path": state["template_path"],
                "template_version": state["template_version"],
            },
            "company": state["company"],
            "eastmoney": state.get("eastmoney_data", {}),
            "xueqiu": state.get("xueqiu_data", {}),
            "official_site": state.get("official_site_data", {}),
            "part_results": state.get("part_results", {}),
            "artifacts": state.get("artifacts", []),
            "node_errors": [*state.get("errors", []), *state.get("node_errors", [])],
        },
    )
    plan = _mark_step(state, "generate_evidence", "completed", "JSON artifacts written")
    save_json(plan_path, plan)
    get_logger().info("Evidence artifacts written to %s", run_dir)
    return {
        "sources_json_path": str(sources_path),
        "failed_fields_json_path": str(failed_path),
        "extracted_data_json_path": str(extracted_path),
        "execution_plan_json_path": str(plan_path),
        "execution_plan": plan,
        "current_step": "generate_evidence",
    }


def _value_is_written(document: Any, mapping: dict[str, Any], value: str) -> bool:
    def paragraph_text(paragraph: Any) -> str:
        return "".join(node.text or "" for node in paragraph._p.xpath(".//w:t"))

    def cell_for(locator: dict[str, Any]) -> Any:
        cell = None
        for depth, step in enumerate(locator["table_path"]):
            tables = document.tables if depth == 0 else cell.tables
            cell = tables[step["table_index"]].cell(step["row_index"], step["column_index"])
        return cell

    for locator in mapping["locators"]:
        kind = locator["kind"]
        if kind == "paragraph":
            found = value in paragraph_text(document.paragraphs[locator["paragraph_index"]])
        elif kind == "header":
            section = document.sections[0]
            header = {
                "default": section.header,
                "first": section.first_page_header,
                "even": section.even_page_header,
            }[locator.get("header_type", "default")]
            found = value in paragraph_text(header.paragraphs[locator["paragraph_index"]])
        elif kind in {"table_cell", "image_anchor"}:
            cell = cell_for(locator)
            found = any(value in paragraph_text(paragraph) for paragraph in cell.paragraphs)
            if mapping["write_strategy"] == "insert_image":
                found = bool(cell._tc.xpath(".//a:blip"))
        else:
            found = False
        if not found:
            return False
    return True


def self_check_node(state: WorkupAgentState) -> dict[str, Any]:
    output_path = Path(state["output_docx_path"])
    issues: list[str] = []
    checks: dict[str, bool] = {}
    checks["result_docx_exists"] = output_path.exists() and output_path.stat().st_size > 0
    configured_ids = {item["field_id"] for item in state["field_mapping"]}
    success_ids = set(state["field_results"])
    failure_ids = {item["field_id"] for item in state["failed_fields"]}
    checks["every_configured_field_accounted_for"] = configured_ids == success_ids | failure_ids
    checks["success_and_failure_disjoint"] = not success_ids & failure_ids
    evidence_ids = {item["field_id"] for item in state["evidence_records"]}
    checks["every_success_has_evidence"] = set(state["field_results"]) == evidence_ids
    checks["every_failure_has_reason"] = all(item.get("reason") for item in state["failed_fields"])
    checks["every_evidence_has_source_url"] = all(
        item.get("source_url") for item in state["evidence_records"]
    )
    checks["json_outputs_exist"] = all(
        Path(state[key]).exists()
        for key in (
            "sources_json_path",
            "failed_fields_json_path",
            "extracted_data_json_path",
            "execution_plan_json_path",
        )
    )
    checks["json_outputs_parse"] = checks["json_outputs_exist"]
    if checks["json_outputs_exist"]:
        try:
            for key in (
                "sources_json_path",
                "failed_fields_json_path",
                "extracted_data_json_path",
                "execution_plan_json_path",
            ):
                with Path(state[key]).open("r", encoding="utf-8") as handle:
                    json.load(handle)
        except (OSError, json.JSONDecodeError):
            checks["json_outputs_parse"] = False
    checks["artifacts_exist"] = all(
        Path(item["path"]).is_file() for item in state.get("artifacts", [])
    )
    artifact_fields = {
        item.get("field_id") for item in state.get("artifacts", []) if item.get("field_id")
    }
    successful_artifact_fields = {
        field_id
        for field_id, result in state.get("field_results", {}).items()
        if result.get("artifact_path")
    }
    checks["artifact_fields_have_results"] = (
        artifact_fields - {"negative_news_attachment"}
    ) <= successful_artifact_fields
    checks["result_artifacts_registered"] = all(
        any(Path(item["path"]).resolve() == Path(result["artifact_path"]).resolve() for item in state.get("artifacts", []))
        for result in state.get("field_results", {}).values()
        if result.get("artifact_path")
    )

    checks["written_values_match_mapping"] = False
    document = None
    if checks["result_docx_exists"]:
        try:
            document = Document(output_path)
        except Exception as exc:
            issues.append(f"DOCX cannot be reopened: {exc}")
    if document is not None:
        mapping_by_id = {item["field_id"]: item for item in state["field_mapping"]}
        mismatches = []
        for field_id, result in state["field_results"].items():
            mapping = mapping_by_id[field_id]
            if mapping["write_strategy"] == "fill_fixed_table":
                values = [str(value) for row in (result.get("structured_value") or []) for value in row]
                document_text = "".join(node.text or "" for node in document.element.xpath(".//w:t"))
                written = bool(values) and all(value in document_text for value in values)
            else:
                written = _value_is_written(document, mapping, result["value"])
            if not written:
                mismatches.append(field_id)
        checks["written_values_match_mapping"] = not mismatches
        if mismatches:
            issues.append(f"DOCX value mismatch: {', '.join(mismatches)}")
        failed_mapping = {
            item["field_id"]: item for item in state["field_mapping"] if item["field_id"] in failure_ids
        }
        failed_targets_preserved = True
        for mapping in failed_mapping.values():
            for locator in mapping["locators"]:
                expected = locator.get("expected_text")
                if expected and not _value_is_written(
                    document,
                    {**mapping, "locators": [locator], "write_strategy": "replace_text"},
                    expected,
                ):
                    failed_targets_preserved = False
                if locator.get("expected_empty"):
                    kind = locator["kind"]
                    if kind == "paragraph":
                        target = document.paragraphs[locator["paragraph_index"]]
                        is_empty = not target.text.strip() and not target._p.xpath(".//a:blip")
                    elif kind == "header":
                        section = document.sections[0]
                        header = {
                            "default": section.header,
                            "first": section.first_page_header,
                            "even": section.even_page_header,
                        }[locator.get("header_type", "default")]
                        target = header.paragraphs[locator["paragraph_index"]]
                        is_empty = not target.text.strip() and not target._p.xpath(".//a:blip")
                    else:
                        cell = None
                        for depth, step in enumerate(locator["table_path"]):
                            tables = document.tables if depth == 0 else cell.tables
                            cell = tables[step["table_index"]].cell(
                                step["row_index"], step["column_index"]
                            )
                        is_empty = not cell.text.strip() and not cell._tc.xpath(".//a:blip")
                    if not is_empty:
                        failed_targets_preserved = False
        checks["failed_targets_preserved"] = failed_targets_preserved
        image_relationships = [
            rel for rel in document.part.rels.values() if rel.reltype.endswith("/image")
        ]
        expected_images = sum(
            mapping["write_strategy"] == "insert_image" and mapping["field_id"] in success_ids
            for mapping in state["field_mapping"]
        )
        checks["images_embedded"] = len(image_relationships) >= expected_images
        remaining_placeholders = find_unmarked_non_mvp_placeholders(
            document,
            state["field_mapping"],
            {
                key: FieldResult.model_validate(value)
                for key, value in state["field_results"].items()
            },
        )
        checks["non_mvp_placeholders_marked"] = not remaining_placeholders
        if remaining_placeholders:
            issues.append(
                "Unmarked non-MVP placeholders: " + "; ".join(remaining_placeholders[:5])
            )
        checks["docx_reopens"] = True
    else:
        checks["failed_targets_preserved"] = False
        checks["images_embedded"] = False
        checks["docx_reopens"] = False

    for name, passed in checks.items():
        if not passed and not any(name in issue for issue in issues):
            issues.append(f"Self-check failed: {name}")
    result = SelfCheckResult(passed=all(checks.values()), checks=checks, issues=issues)
    plan = _mark_step(state, "self_check", "completed" if result.passed else "failed", "; ".join(issues))
    save_json(Path(state["execution_plan_json_path"]), plan)
    get_logger().info("Self-check passed=%s", result.passed)
    return {
        "self_check_result": result.model_dump(mode="json"),
        "execution_plan": plan,
        "current_step": "self_check",
    }


def finalize_run_node(state: WorkupAgentState) -> dict[str, Any]:
    success_count = len(state["field_results"])
    failed_count = len(state["failed_fields"])
    get_logger().info("Run finalized: %d success, %d failed", success_count, failed_count)
    print(f"\nRun directory: {state['run_dir']}")
    print(f"Successful fields: {success_count}")
    print(f"Failed fields: {failed_count}")
    print(f"Result DOCX: {state['output_docx_path']}")
    plan = _mark_step(state, "finalize", "completed", "Run completed")
    save_json(Path(state["execution_plan_json_path"]), plan)
    return {
        "current_step": "finalize",
        "execution_plan": plan,
    }
