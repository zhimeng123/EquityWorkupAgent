from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re
from typing import Any, Callable

from openai import OpenAI

from mlc_agent.audit_changes import Part11Input, collect_part11
from mlc_agent.charts import stock_chart_node
from mlc_agent.company_supplement import (
    EvidenceDocument as Part01Document,
    collect_company_supplement,
    make_openai_extractor as make_part01_extractor,
)
from mlc_agent.company_resolver import build_eastmoney_url
from mlc_agent.deep_financial_analysis import collect_deep_financial_analysis
from mlc_agent.external_links import build_cninfo_company_url, collect_external_links
from mlc_agent.financial_metrics import collect_financial_metrics
from mlc_agent.governance import GovernanceReportInput, collect_governance
from mlc_agent.http_client import build_http_client
from mlc_agent.llm import get_llm_api_key, get_llm_base_url, get_llm_model
from mlc_agent.operating_performance import (
    FinancialPeriodRecord,
    OutlookRiskItem,
    build_outlook_english_prompt,
    collect_operating_performance,
)
from mlc_agent.peer_analysis import collect_peer_analysis
from mlc_agent.production_adapters import (
    Part06Extraction,
    Part07Extraction,
    Part07ReceivablesExtraction,
    Part08Extraction,
    Part10ChangesExtraction,
    Part10CoreExtraction,
    Part10ShareholdersExtraction,
    Part11AuditExtraction,
    Part11BoardChangesExtraction,
    Part11BusinessChangesExtraction,
    Part11ChangesExtraction,
    Part11LegalExtraction,
    Part11LitigationExtraction,
    Part11NewsExtraction,
    Part11RegulatoryExtraction,
    Part11RestatementExtraction,
    Part11ShareholderChangesExtraction,
    SharedDisclosureBundle,
    collect_fixed_peer_companies,
    collect_shared_disclosures,
    discover_negative_news,
    evidence_text_documents,
    extract_part05_related_party_transactions,
    filtered_disclosure_payload,
    extract_pydantic,
    operating_report_documents,
)


_PART01_PAGES = re.compile(r"实际控制人|上市子公司|董事|收购|并购|重组|未来.{0,12}(计划|展望)")
_PART01_MA_TITLES = re.compile(r"收购|并购|重组")
_PART03_PAGES = re.compile(
    r"美国|USA|United States|California|Texas|美洲|境外|子公司|分地区|地区收入|员工|雇员"
)
_PART06_PAGES = re.compile(r"资产负债表|现金流量表|利润表|货币资金|短期借款|存货|利息费用|购建固定资产")
_PART07_PAGES = re.compile(r"应收账款|坏账|商誉|无形资产|资产减值|信用减值")
_PART08_TITLES = re.compile(r"发行|配股|增发|可转债|上市")
_PART08_PAGES = re.compile(r"首次公开发行|发行证券|配股|增发|可转换公司债券|募集资金")
_PART10_PAGES = re.compile(r"董事|高级管理人员|监事|股东|实际控制人|员工|雇员")
_PART10_CORE_PAGES = re.compile(r"董事|高级管理人员|监事|员工|雇员")
_PART10_SHAREHOLDER_PAGES = re.compile(r"股东|持股")
_PART10_CHANGE_PAGES = re.compile(r"实际控制人|控股股东|变更|辞任|聘任")
_PART11_TITLES = re.compile(r"审计|会计师|更正|重述|诉讼|仲裁|处罚|监管")
_PART11_PAGES = re.compile(
    r"审计|会计师|更正|重述|董事|高级管理人员|经营|业务|前三名股东|前3名股东|股东|诉讼|仲裁|处罚|整改|监管"
)
_PART11_AUDIT_PAGES = re.compile(r"审计报告|审计意见|会计师")
_PART11_CHANGE_PAGES = re.compile(r"更正|重述|董事|高级管理人员|经营|业务|前三名股东|前3名股东")
_PART11_LEGAL_PAGES = re.compile(r"诉讼|仲裁|处罚|监管")
_PART11_AUDIT_TITLES = re.compile(r"审计|会计师")
_PART11_CHANGE_TITLES = re.compile(r"更正|重述|董事|高级管理人员|经营|业务|股东")
_PART11_LEGAL_TITLES = re.compile(r"诉讼|仲裁|处罚|监管")
from mlc_agent.related_parties import collect_related_party_transactions
from mlc_agent.schemas import CompanyIdentity, WorkupAgentState
from mlc_agent.security_analysis import collect_security_analysis
from mlc_agent.market_history import market_history
from mlc_agent.us_exposure import (
    EvidenceDocument as Part03Document,
    collect_us_exposure,
    make_openai_extractor as make_part03_extractor,
)


def _as_of(state: WorkupAgentState) -> date:
    return datetime.fromisoformat(state["created_at"]).date()


def _llm_client() -> OpenAI:
    return OpenAI(api_key=get_llm_api_key(), base_url=get_llm_base_url(), timeout=60.0)


def _bundle(state: WorkupAgentState) -> SharedDisclosureBundle:
    raw = state.get("part_results", {}).get("shared_disclosures")
    if not isinstance(raw, dict):
        raise ValueError("shared disclosure bundle is unavailable")
    if raw.get("status") == "failed":
        reason = str(raw.get("reason") or "unknown error")
        raise ValueError(f"shared disclosures failed: {reason}")
    return SharedDisclosureBundle.model_validate(raw)


def _append_result(
    state: WorkupAgentState,
    *,
    part: str,
    raw_result: dict[str, Any],
    source_values: list[Any],
    errors: list[str],
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    part_results = dict(state.get("part_results", {}))
    part_results[part] = raw_result
    plan = [dict(item) for item in state.get("execution_plan", [])]
    for item in plan:
        if item["step_id"] == part:
            item["status"] = "failed" if raw_result.get("status") == "failed" else "completed"
            item["detail"] = raw_result.get("reason") or f"{len(source_values)} field candidates"
            break
    return {
        "part_results": part_results,
        "source_values": list(state.get("source_values", []))
        + [item.model_dump(mode="json") for item in source_values],
        "node_errors": list(state.get("node_errors", []))
        + [{"node": part, "message": item} for item in errors],
        "artifacts": list(state.get("artifacts", [])) + list(artifacts or []),
        "execution_plan": plan,
    }


def _failed(state: WorkupAgentState, part: str, exc: Exception) -> dict[str, Any]:
    reason = str(exc)
    return _append_result(
        state,
        part=part,
        raw_result={"status": "failed", "reason": reason},
        source_values=[],
        errors=[reason],
    )


def collect_shared_disclosures_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        with build_http_client(timeout_seconds=60) as client:
            result = collect_shared_disclosures(
                client,
                company=CompanyIdentity.model_validate(state["company"]),
                as_of=_as_of(state),
                run_dir=Path(state["run_dir"]),
            )
        part_results = dict(state.get("part_results", {}))
        part_results["shared_disclosures"] = result.model_dump(mode="json")
        return {
            "part_results": part_results,
            "report_catalog": [item.model_dump(mode="json") for item in result.announcement_catalog],
            "announcement_catalog": [item.model_dump(mode="json") for item in result.announcement_catalog],
            "node_errors": list(state.get("node_errors", []))
            + [{"node": "shared_disclosures", "message": item} for item in result.errors],
            "execution_plan": [
                {
                    **item,
                    "status": "completed" if item["step_id"] == "shared_disclosures" else item["status"],
                    "detail": (
                        f"{len(result.documents)} parsed disclosures"
                        if item["step_id"] == "shared_disclosures"
                        else item.get("detail")
                    ),
                }
                for item in state.get("execution_plan", [])
            ],
        }
    except Exception as exc:
        return _failed(state, "shared_disclosures", exc)


def part01_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        bundle = _bundle(state)
        evidence = evidence_text_documents(
            bundle,
            document_types={"annual_report", "interim_report"},
            page_pattern=_PART01_PAGES,
            adjacent_pages=2,
            latest_per_type=True,
        ) + evidence_text_documents(
            bundle,
            document_types={"announcement"},
            title_pattern=_PART01_MA_TITLES,
            page_pattern=_PART01_PAGES,
            adjacent_pages=1,
        )
        documents = [
            Part01Document.model_validate(item)
            for item in evidence
        ]
        result = collect_company_supplement(
            documents,
            as_of=_as_of(state),
            extractor=make_part01_extractor(_llm_client(), model=get_llm_model()),
        )
        return _append_result(
            state,
            part="part_01",
            raw_result=result.model_dump(mode="json"),
            source_values=result.source_values,
            errors=[f"{item.field_id}: {item.reason}" for item in result.errors],
        )
    except Exception as exc:
        return _failed(state, "part_01", exc)


def part02_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        bundle = _bundle(state)
        with build_http_client() as client:
            result = collect_external_links(
                client,
                company=CompanyIdentity.model_validate(state["company"]),
                cninfo_org_id=bundle.org_id,
                captured_at=datetime.fromisoformat(state["created_at"]),
            )
        return _append_result(
            state,
            part="part_02",
            raw_result=result.model_dump(mode="json"),
            source_values=result.source_values,
            errors=result.errors,
        )
    except Exception as exc:
        return _failed(state, "part_02", exc)


def part03_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        documents = [
            Part03Document.model_validate(item)
            for item in evidence_text_documents(
                _bundle(state),
                reports_only=True,
                page_pattern=_PART03_PAGES,
                latest_per_type=True,
            )
        ]
        result = collect_us_exposure(
            documents,
            as_of=_as_of(state),
            extractor=make_part03_extractor(_llm_client(), model=get_llm_model()),
        )
        return _append_result(
            state,
            part="part_03",
            raw_result=result.model_dump(mode="json"),
            source_values=result.source_values,
            errors=[f"{item.field_id}: {item.reason}" for item in result.errors],
        )
    except Exception as exc:
        return _failed(state, "part_03", exc)


def _outlook_renderer(client: OpenAI) -> Callable[[tuple[OutlookRiskItem, ...]], str]:
    def render(items: tuple[OutlookRiskItem, ...]) -> str:
        response = client.chat.completions.create(
            model=get_llm_model(),
            messages=[{"role": "user", "content": build_outlook_english_prompt(items)}],
            temperature=0,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("outlook English renderer returned empty text")
        return content

    return render


def part04_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        bundle = _bundle(state)
        records = state.get("eastmoney_data", {}).get("period_records", [])
        result = collect_operating_performance(
            company=CompanyIdentity.model_validate(state["company"]),
            raw_financial_records=records,
            report_documents=operating_report_documents(bundle),
            captured_at=datetime.fromisoformat(state["created_at"]),
            outlook_english_renderer=_outlook_renderer(_llm_client()),
        )
        return _append_result(
            state,
            part="part_04",
            raw_result=result.model_dump(mode="json"),
            source_values=result.source_values,
            errors=result.errors,
        )
    except Exception as exc:
        return _failed(state, "part_04", exc)


def part05_node(state: WorkupAgentState) -> dict[str, Any]:
    # Peer analysis is the upstream contract for Parts 08/09.  Related-party
    # extraction fills an independent report field and must not invalidate that
    # already-completed contract when its LLM request fails.
    try:
        bundle = _bundle(state)
        with build_http_client(timeout_seconds=60) as client:
            target, candidates, identities = collect_fixed_peer_companies(
                client,
                target=CompanyIdentity.model_validate(state["company"]),
                config_dir=Path(state["config_dir"]),
            )
        canonical_annual = [
            FinancialPeriodRecord.model_validate(item)
            for item in state.get("part_results", {}).get("part_04", {}).get("annual_records", [])
        ]
        if len(canonical_annual) < 2:
            raise ValueError("Part 04 did not provide two canonical annual records for Part 05")
        target = target.model_copy(update={"annual_records": canonical_annual})
        result = collect_peer_analysis(
            target=target,
            candidates=candidates,
            captured_at=datetime.fromisoformat(state["created_at"]),
        )
        if not result.selection.success or len(result.selection.selected) != 3:
            raise ValueError(
                result.selection.failure_reason
                or "Part 05 requires exactly three eligible selected peers"
            )
        if result.failures or len(result.source_values) != 2:
            raise ValueError(
                "; ".join(result.failures)
                or "Part 05 peer analysis did not produce both required peer fields"
            )
        values = list(result.source_values)
        identity_by_code = {item.stock_code: item for item in identities}
        selected_identities = [identity_by_code[item.stock_code] for item in result.selection.selected]
        raw = result.model_dump(mode="json")
        raw["peer_analysis_status"] = "completed"
        raw["selected_identities"] = [item.model_dump(mode="json") for item in selected_identities]
    except Exception as exc:
        return _failed(state, "part_05", exc)

    errors: list[str] = []
    try:
        related_transactions = extract_part05_related_party_transactions(
            _llm_client(),
            model=get_llm_model(),
            bundle=bundle,
        )
        related_value = collect_related_party_transactions(
            related_transactions,
            captured_at=datetime.fromisoformat(state["created_at"]),
        )
        if related_value is None:
            raise ValueError("no verified transaction disclosure")
        values.append(related_value)
        raw["related_party_status"] = "completed"
        raw["status"] = "completed"
    except Exception as exc:
        reason = f"related_party_transactions: {exc}"
        raw["related_party_status"] = "failed"
        raw["related_party_error"] = str(exc)
        raw["status"] = "partial"
        raw["reason"] = reason
        errors.append(reason)

    try:
        return _append_result(state, part="part_05", raw_result=raw, source_values=values, errors=errors)
    except Exception as exc:  # Serialization/state update is a Part 05 failure.
        return _failed(state, "part_05", exc)


def part06_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        bundle = _bundle(state)
        operating = state.get("part_results", {}).get("part_04", {})
        extraction = extract_pydantic(
            _llm_client(), model=get_llm_model(), output_model=Part06Extraction,
            system_prompt=(
                "Extract the two complete annual liquidity records and latest balance-sheet debt inputs using "
                "the exact requested statement line items. Every MetricInput.period for an annual record MUST "
                "be formatted exactly as FY followed by its four-digit fiscal year, for example FY2025, never "
                "as a calendar date. LatestBalanceSheetInput.period and both nested metric periods must be identical."
            ),
            payload={
                "annual_records": operating.get("annual_records", []),
                "documents": filtered_disclosure_payload(
                    bundle,
                    document_types={"annual_report"},
                    page_pattern=_PART06_PAGES,
                ),
            },
        )
        canonical_by_year = {
            item.fiscal_year: item
            for item in (
                FinancialPeriodRecord.model_validate(raw)
                for raw in operating.get("annual_records", [])
            )
        }
        annual_inputs = []
        for extracted in extraction.annual_inputs:
            year = extracted.financial_period.fiscal_year
            if year not in canonical_by_year:
                raise ValueError(f"Part 04 canonical annual record missing for FY{year}")
            annual_inputs.append(
                extracted.model_copy(update={"financial_period": canonical_by_year[year]})
            )
        result = collect_financial_metrics(
            annual_records=annual_inputs,
            latest_balance_sheet=extraction.latest_balance,
            captured_at=datetime.fromisoformat(state["created_at"]),
        )
        return _append_result(
            state,
            part="part_06",
            raw_result=result.model_dump(mode="json"),
            source_values=result.source_values,
            errors=(
                extraction.validation_errors
                + [f"{item.field_id}: {item.reason}" for item in result.failures]
            ),
        )
    except Exception as exc:
        return _failed(state, "part_06", exc)


def part07_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        bundle = _bundle(state)
        operating = state.get("part_results", {}).get("part_04", {})
        documents = filtered_disclosure_payload(
            bundle,
            document_types={"annual_report"},
            page_pattern=_PART07_PAGES,
            latest_per_type=True,
        )
        extraction = extract_pydantic(
            _llm_client(), model=get_llm_model(), output_model=Part07Extraction,
            system_prompt="Extract the latest annual balance-sheet details and only eligible receivable/contract-asset impairment components.",
            payload={
                "annual_records": operating.get("annual_records", []),
                "documents": documents,
            },
        )
        receivable_details = []
        receivables_error = None
        try:
            receivables = extract_pydantic(
                _llm_client(), model=get_llm_model(), output_model=Part07ReceivablesExtraction,
                system_prompt=(
                    "Extract exactly the latest two consecutive fiscal-year accounts-receivable carrying amounts. "
                    "Use the current and comparative columns in the latest annual report; both records must use the "
                    "same consolidation scope id. Do not extract notes receivable or other receivables."
                ),
                payload={
                    "annual_records": operating.get("annual_records", []),
                    "documents": documents,
                },
            )
            receivable_details = receivables.receivable_details
        except Exception as exc:
            receivables_error = f"receivables_vs_revenue_growth extraction: {exc}"
        result = collect_deep_financial_analysis(
            annual_records=[FinancialPeriodRecord.model_validate(item) for item in operating.get("annual_records", [])],
            balance_sheet_details=extraction.balance_sheet_details,
            receivable_details=receivable_details,
            impairment_components=extraction.impairment_components,
            captured_at=datetime.fromisoformat(state["created_at"]),
        )
        errors = ([receivables_error] if receivables_error else []) + result.errors
        return _append_result(state, part="part_07", raw_result=result.model_dump(mode="json"), source_values=result.source_values, errors=errors)
    except Exception as exc:
        return _failed(state, "part_07", exc)


def part08_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        bundle = _bundle(state)
        part05 = state.get("part_results", {}).get("part_05", {})
        if part05.get("peer_analysis_status") != "completed":
            raise ValueError(
                f"Part 05 peer analysis failed: {part05.get('reason') or 'unknown error'}"
            )
        peer_identities = [CompanyIdentity.model_validate(item) for item in part05.get("selected_identities", [])]
        if len(peer_identities) != 3:
            raise ValueError("Part 05 did not provide exactly three selected peer identities")
        extraction = extract_pydantic(
            _llm_client(), model=get_llm_model(), output_model=Part08Extraction,
            system_prompt="Extract IPO evidence and every securities offering in the exact rolling 12-month announcement window.",
            payload={
                "as_of": _as_of(state),
                "catalog_source_url": build_cninfo_company_url(
                    stock_code=state["company"]["stock_code"],
                    org_id=bundle.org_id,
                ),
                "structured_ipo_candidate": {
                    "listing_date": state.get("eastmoney_data", {}).get("profile", {}).get("LISTING_DATE"),
                    "source_url": build_eastmoney_url(
                        CompanyIdentity.model_validate(state["company"])
                    ),
                },
                "documents": filtered_disclosure_payload(
                    bundle,
                    title_pattern=_PART08_TITLES,
                    page_pattern=_PART08_PAGES,
                ),
            },
        )
        with build_http_client(timeout_seconds=60) as client:
            history = market_history(
                client,
                target=CompanyIdentity.model_validate(state["company"]),
                peers=peer_identities,
                as_of=_as_of(state),
            )
        result = collect_security_analysis(
            market=history,
            ipo_candidates=extraction.ipo_candidates,
            offering_review=extraction.offering_review,
            as_of=_as_of(state),
            captured_at=datetime.fromisoformat(state["created_at"]),
        )
        return _append_result(state, part="part_08", raw_result=result.model_dump(mode="json"), source_values=result.source_values, errors=result.errors)
    except Exception as exc:
        return _failed(state, "part_08", exc)


def part09_node(state: WorkupAgentState) -> dict[str, Any]:
    output = stock_chart_node(state)
    result = output.get("part_results", {}).get("part_09", {})
    errors = result.get("errors", []) if isinstance(result, dict) else []
    plan = [dict(item) for item in state.get("execution_plan", [])]
    for item in plan:
        if item["step_id"] == "part_09":
            item["status"] = "failed" if errors else "completed"
            item["detail"] = (
                "; ".join(str(error.get("reason")) for error in errors)
                if errors
                else "2 chart artifacts generated"
            )
            break
    output["execution_plan"] = plan
    return output


def part10_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        bundle = _bundle(state)
        client = _llm_client()
        base = {"as_of": _as_of(state), "captured_at": state["created_at"]}
        core = extract_pydantic(
            client, model=get_llm_model(), output_model=Part10CoreExtraction,
            system_prompt="Extract report metadata, current people and roles, board structure, and employees only.",
            payload={**base, "documents": filtered_disclosure_payload(
                bundle, document_types={"annual_report"}, page_pattern=_PART10_CORE_PAGES,
                adjacent_pages=1, latest_per_type=True,
            )},
        )
        shareholders = extract_pydantic(
            client, model=get_llm_model(), output_model=Part10ShareholdersExtraction,
            system_prompt=(
                "Extract current and prior shareholder snapshots only. Every nested shareholder.report_period "
                "must equal its snapshot report_period. The current snapshot must use the supplied annual report period."
            ),
            payload={**base, "report_period": core.report_period, "documents": filtered_disclosure_payload(
                bundle, document_types={"annual_report"}, page_pattern=_PART10_SHAREHOLDER_PAGES,
                adjacent_pages=1, latest_per_type=True,
            )},
        )
        changes = extract_pydantic(
            client, model=get_llm_model(), output_model=Part10ChangesExtraction,
            system_prompt="Extract controller status and explicitly disclosed post-report governance/shareholder changes only.",
            payload={**base, "documents": filtered_disclosure_payload(
                bundle, document_types={"annual_report"}, page_pattern=_PART10_CHANGE_PAGES,
                adjacent_pages=1, latest_per_type=True,
            )},
        )
        data = GovernanceReportInput(
            **core.model_dump(),
            captured_at=datetime.fromisoformat(state["created_at"]),
            **shareholders.model_dump(),
            **changes.model_dump(),
        )
        result = collect_governance(data)
        return _append_result(state, part="part_10", raw_result=result.result.model_dump(mode="json"), source_values=result.source_values, errors=[])
    except Exception as exc:
        return _failed(state, "part_10", exc)


def part11_node(state: WorkupAgentState) -> dict[str, Any]:
    try:
        bundle = _bundle(state)
        with build_http_client(timeout_seconds=30) as client:
            news = discover_negative_news(
                client,
                company=CompanyIdentity.model_validate(state["company"]),
                as_of=_as_of(state),
            )
        client = _llm_client()
        common_instruction = (
            "Use annual-report Chinese exact quotations directly. Classify each topic as: yes only when "
            "the report discloses the fact; no only when the quotation itself explicitly says 不存在、未发生、"
            "无重大、不适用 or an equivalent negative; not_disclosed only when the supplied formal topic "
            "section was reviewed but does not separately disclose the requested fact. For not_disclosed, "
            "coverage_evidence must quote that topic section. Never use not_disclosed for extraction or network failures."
        )
        calls = (
            ("audit", Part11AuditExtraction, "Extract the auditor and audit opinion from each supplied annual report. "
             "For an unmodified opinion, modified_opinion_details must be null. Compare auditors only when two "
             "consecutive annual reports are supplied; do not infer post-report changes from an annual report. " + common_instruction,
             {"documents": filtered_disclosure_payload(bundle, document_types={"annual_report"}, page_pattern=_PART11_AUDIT_PAGES)}),
            ("restatements", Part11RestatementExtraction, "Extract financial restatements only. " + common_instruction,
             {"documents": filtered_disclosure_payload(bundle, document_types={"annual_report"}, page_pattern=re.compile(r"更正|重述"), adjacent_pages=1, latest_per_type=True),
              "matter_documents": filtered_disclosure_payload(bundle, document_types={"announcement"}, title_pattern=re.compile(r"更正|重述"), page_pattern=re.compile(r"更正|重述"), adjacent_pages=1)}),
            ("board_changes", Part11BoardChangesExtraction, "Extract disclosed director changes (person, role, date and reason) from the annual-report personnel-change table. "
             "For this MVP, a director change explicitly listed in that table satisfies explicit_material=true. " + common_instruction,
             {"documents": filtered_disclosure_payload(bundle, document_types={"annual_report"}, page_pattern=re.compile(r"董事|高级管理人员"), adjacent_pages=1, latest_per_type=True)}),
            ("business_changes", Part11BusinessChangesExtraction, "Extract explicit material business-operation changes only. " + common_instruction,
             {"documents": filtered_disclosure_payload(bundle, document_types={"annual_report"}, page_pattern=re.compile(r"经营|业务"), adjacent_pages=1, latest_per_type=True)}),
            ("shareholder_changes", Part11ShareholderChangesExtraction, "Compare the top-three shareholder snapshots only when at least two report periods are supplied. "
             "A single annual report cannot prove a change or no-change result; in that case return not_disclosed with coverage evidence. "
             "Do not infer post-report shareholder changes. " + common_instruction,
             {"documents": filtered_disclosure_payload(bundle, document_types={"annual_report"}, page_pattern=re.compile(r"前三名股东|前3名股东|股东"), adjacent_pages=1)}),
            ("litigation", Part11LitigationExtraction, "Distinguish 'no major litigation' from other disclosed litigation. "
             "If the annual report says no major litigation but lists other litigation, return yes and extract those matters; do not collapse it to no. " + common_instruction,
             {"documents": filtered_disclosure_payload(bundle, document_types={"annual_report"}, page_pattern=re.compile(r"诉讼|仲裁"), adjacent_pages=1, latest_per_type=True),
              "matter_documents": filtered_disclosure_payload(bundle, document_types={"announcement"}, title_pattern=re.compile(r"诉讼|仲裁"), page_pattern=re.compile(r"诉讼|仲裁"), adjacent_pages=1)}),
            ("regulatory", Part11RegulatoryExtraction, "Extract penalties and rectification from the annual-report penalties/rectification section. "
             "Return no only when that section explicitly states none/not applicable. Do not infer post-report regulatory events from one annual report. " + common_instruction,
             {"documents": filtered_disclosure_payload(bundle, document_types={"annual_report"}, page_pattern=re.compile(r"处罚|监管"), adjacent_pages=1, latest_per_type=True),
              "matter_documents": filtered_disclosure_payload(bundle, document_types={"announcement"}, title_pattern=re.compile(r"处罚|监管"), page_pattern=re.compile(r"处罚|监管"), adjacent_pages=1)}),
            ("news", Part11NewsExtraction, "Classify only company-specific adverse news. Search URLs are discovery only; use original URLs. If body is unavailable leave body and summary null.",
             {"news_candidates": news}),
        )
        extracted: dict[str, Any] = {}
        group_errors: list[str] = []
        for group, output_model, prompt, group_payload in calls:
            try:
                extracted[group] = extract_pydantic(
                    client, model=get_llm_model(), output_model=output_model,
                    system_prompt=prompt,
                    payload={"as_of": _as_of(state), **group_payload},
                )
            except Exception as exc:
                group_errors.append(f"{group}: {exc}")
        if group_errors:
            # Preserve successful isolated group outputs; never invent required data
            # merely to force the aggregate Part11Input through validation.
            return _append_result(
                state,
                part="part_11",
                raw_result={
                    "status": "partial",
                    "groups": {key: value.model_dump(mode="json") for key, value in extracted.items()},
                    "failed_groups": group_errors,
                },
                source_values=[],
                errors=group_errors,
            )
        audit = extracted["audit"]
        news_result = extracted["news"]
        documents = evidence_text_documents(
            bundle, document_types={"annual_report"}, page_pattern=_PART11_PAGES
        )
        matter_documents = evidence_text_documents(
            bundle, document_types={"announcement"},
            title_pattern=_PART11_TITLES, page_pattern=_PART11_PAGES,
        )
        data = Part11Input(
            documents=documents,
            matter_documents=matter_documents,
            latest_audit=audit.latest_audit,
            previous_audit=audit.previous_audit,
            post_report_auditor_changes=audit.post_report_auditor_changes,
            restatements=extracted["restatements"].restatements,
            board_officer_changes=extracted["board_changes"].board_officer_changes,
            business_operation_changes=extracted["business_changes"].business_operation_changes,
            top3_shareholder_changes=extracted["shareholder_changes"].top3_shareholder_changes,
            litigation=extracted["litigation"].litigation,
            regulatory=extracted["regulatory"].regulatory,
            news_articles=news_result.news_articles,
        )
        result = collect_part11(
            data,
            company_name=state["company"]["company_name"],
            as_of=_as_of(state),
            captured_at=datetime.fromisoformat(state["created_at"]),
            artifact_dir=Path(state["run_dir"]),
        )
        return _append_result(
            state, part="part_11", raw_result=result.model_dump(mode="json"),
            source_values=result.source_values,
            errors=[f"{item.field_id}: {item.reason}" for item in result.errors],
            artifacts=[item.model_dump(mode="json") for item in result.artifacts],
        )
    except Exception as exc:
        return _failed(state, "part_11", exc)
