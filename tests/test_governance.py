from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import TypeAdapter

from mlc_agent.config import load_yaml
from mlc_agent.docx_writer import validate_template_mapping
from mlc_agent.governance import (
    BoardStructure,
    ControllerChange,
    EmployeeBreakdown,
    GovernanceChange,
    GovernanceReportInput,
    Person,
    Role,
    Shareholder,
    ShareholderChangeEvent,
    ShareholderSnapshot,
    collect_governance,
    collect_governance_node,
    compare_shareholder_snapshots,
)
from mlc_agent.schemas import FieldMapping


ROOT = Path(__file__).resolve().parents[1]
REPORT_URL = "https://static.cninfo.com.cn/report.pdf"


def _shareholders(period: str, *, changed: bool = False) -> list[Shareholder]:
    output = []
    for rank in range(1, 11):
        name = f"股东{rank}"
        if changed and rank == 10:
            name = "新股东"
        output.append(
            Shareholder(
                rank=rank,
                name=name,
                shareholding_percent=20.0 / rank,
                insider_status=True if rank == 1 else "Not disclosed",
                report_period=period,
                source_page=80,
            )
        )
    return output


def _input() -> GovernanceReportInput:
    chairman = Person(
        name="张三",
        roles=[
            Role(category="director", title="Chairman"),
            Role(category="executive", title="CEO"),
        ],
        education="MBA",
        work_experience="Technology management",
        source_page=50,
    )
    return GovernanceReportInput(
        report_period="2025-12-31",
        report_type="annual_report",
        source_url=REPORT_URL,
        captured_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        report_date=date(2025, 12, 31),
        persons=[
            chairman,
            chairman.model_copy(deep=True),
            Person(
                name="李四",
                roles=[Role(category="supervisor", title="Supervisor")],
                source_page=52,
            ),
            Person(
                name="李四",
                roles=[Role(category="director", title="Director")],
                source_page=53,
            ),
        ],
        board_structure=BoardStructure(
            director_total=9,
            independent_directors=3,
            supervisory_board=True,
            audit_committee=True,
            compensation_committee=True,
            source_page=60,
        ),
        current_shareholders=ShareholderSnapshot(
            report_period="2025-12-31",
            reporting_basis="ordinary shares",
            shareholders=_shareholders("2025-12-31", changed=True),
        ),
        previous_shareholders=ShareholderSnapshot(
            report_period="2024-12-31",
            reporting_basis="ordinary shares",
            shareholders=_shareholders("2024-12-31"),
        ),
        post_report_changes=[
            GovernanceChange(
                effective_date=date(2025, 12, 1),
                person_name="Old",
                role="Director",
                change="resigned",
                details="Before report date",
                source_url=REPORT_URL,
            ),
            GovernanceChange(
                effective_date=date(2026, 3, 1),
                person_name="New",
                role="CFO",
                change="appointed",
                details="Board appointment",
                source_url="https://static.cninfo.com.cn/announcement.pdf",
            ),
        ],
        post_report_shareholder_changes=[
            ShareholderChangeEvent(
                effective_date=date(2026, 4, 1),
                shareholder_name="股东1",
                details="Disclosed reduction in shareholding.",
                source_url="https://static.cninfo.com.cn/shareholder-announcement.pdf",
            )
        ],
        controller_change=ControllerChange(
            changed=False,
            source="cninfo",
            source_url="https://static.cninfo.com.cn/controller-announcement.pdf",
            evidence_date=date(2026, 5, 1),
        ),
        employees=EmployeeBreakdown(
            prc=90,
            us=5,
            europe=3,
            other=1,
            total=100,
            source_page=90,
        ),
    )


def test_people_are_deduplicated_without_merging_same_name_different_people():
    collection = collect_governance(_input())
    result = collection.result
    assert len(result.persons) == 3
    assert [person.name for person in result.persons].count("李四") == 2
    assert [person.name for person in result.current_directors] == ["张三", "李四"]
    assert [person.name for person in result.executives] == ["张三"]
    assert [person.name for person in result.supervisors] == ["李四"]


def test_undisclosed_executive_director_count_is_not_inferred():
    collection = collect_governance(_input())
    assert collection.result.board_structure.executive_directors == "Not disclosed"
    value = next(item for item in collection.source_values if item.field_id == "board_structure")
    assert "Executive Directors: Not disclosed" in value.value


def test_top_ten_shareholders_use_one_period_and_do_not_infer_insiders():
    collection = collect_governance(_input())
    shareholders = collection.result.major_shareholders
    assert len(shareholders) == 10
    assert {item.report_period for item in shareholders} == {"2025-12-31"}
    assert shareholders[0].insider_status is True
    assert all(item.insider_status == "Not disclosed" for item in shareholders[1:])
    value = next(item for item in collection.source_values if item.field_id == "major_shareholders")
    assert len(value.item_evidence) == 10
    assert "Insider: Not disclosed" in value.value


def test_shareholder_comparison_requires_same_reporting_basis():
    current = _input().current_shareholders
    previous = _input().previous_shareholders.model_copy(update={"reporting_basis": "A shares only"})
    assert compare_shareholder_snapshots(current, previous).status == "Not disclosed"
    changed = compare_shareholder_snapshots(current, _input().previous_shareholders)
    assert changed.status == "Yes"
    assert any("新股东 entered" in detail for detail in changed.details)


def test_employee_discrepancy_is_recorded_without_adjustment():
    collection = collect_governance(_input())
    assert collection.result.employees.total == 100
    assert collection.result.employees.disclosed_region_sum == 99
    assert collection.result.employee_discrepancy == 1
    total = next(item for item in collection.source_values if item.field_id == "employees_total")
    assert total.value == "100"


def test_controller_and_general_shareholder_changes_remain_separate():
    collection = collect_governance(_input())
    assert collection.result.shareholder_changes.status == "Yes"
    assert collection.result.controller_change.changed is False
    assert len(collection.result.post_report_shareholder_changes) == 1
    assert any(
        "Disclosed reduction in shareholding" in detail
        for detail in collection.result.shareholder_changes.details
    )
    assert len(collection.result.director_officer_changes) == 1
    assert collection.result.director_officer_changes[0].person_name == "New"
    details = next(
        item for item in collection.source_values if item.field_id == "major_shareholder_change_details"
    )
    assert details.item_evidence[0].source_url.endswith("shareholder-announcement.pdf")


def test_controller_change_uses_its_own_announcement_evidence():
    collection = collect_governance(_input())
    value = next(item for item in collection.source_values if item.field_id == "controller_change")
    assert value.value == "No"
    assert value.source == "cninfo"
    assert value.source_url.endswith("controller-announcement.pdf")
    assert value.source_url != REPORT_URL
    assert value.period == "2026-05-01"


def test_node_wrapper_returns_structured_result_and_source_values():
    data = _input()
    output = collect_governance_node(
        {
            "part_results": {"part_10_input": data.model_dump(mode="json")},
            "source_values": [],
        }
    )
    assert output["part_results"]["part_10"]["employees"]["total"] == 100
    assert {item["field_id"] for item in output["source_values"]} >= {
        "board_structure",
        "major_shareholders",
        "controller_change",
        "employees_total",
    }


def test_part_10_mapping_is_valid_for_real_template():
    fragment = load_yaml(ROOT / "configs" / "fields" / "part_10.yaml")
    mappings = TypeAdapter(list[FieldMapping]).validate_python(fragment["fields"])
    validate_template_mapping(
        ROOT / "Workup_template_260617-外测版.docx",
        [item.model_dump(mode="json") for item in mappings],
    )
