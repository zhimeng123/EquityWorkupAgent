from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from mlc_agent.schemas import ItemEvidence, SourceName, SourceValue, WorkupAgentState


NOT_DISCLOSED = "Not disclosed"
DisclosureBool = bool | Literal["Not disclosed"]


class Role(BaseModel):
    category: Literal["director", "executive", "supervisor"]
    title: str = Field(min_length=1)
    current: bool = True


class Person(BaseModel):
    name: str = Field(min_length=1)
    roles: list[Role] = Field(min_length=1)
    education: str | None = None
    qualifications: str | None = None
    work_experience: str | None = None
    source_page: int = Field(ge=1)

    @model_validator(mode="after")
    def unique_roles(self) -> "Person":
        keys = [(role.category, role.title, role.current) for role in self.roles]
        if len(keys) != len(set(keys)):
            raise ValueError("person contains duplicate roles")
        return self


class BoardStructure(BaseModel):
    director_total: int = Field(ge=0)
    executive_directors: int | Literal["Not disclosed"] = NOT_DISCLOSED
    independent_directors: int = Field(ge=0)
    supervisory_board: DisclosureBool = NOT_DISCLOSED
    audit_committee: DisclosureBool = NOT_DISCLOSED
    compensation_committee: DisclosureBool = NOT_DISCLOSED
    source_page: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_counts(self) -> "BoardStructure":
        if self.independent_directors > self.director_total:
            raise ValueError("independent directors cannot exceed total directors")
        if isinstance(self.executive_directors, int) and self.executive_directors > self.director_total:
            raise ValueError("executive directors cannot exceed total directors")
        return self


class GovernanceChange(BaseModel):
    effective_date: date
    person_name: str
    role: str
    change: Literal["appointed", "resigned", "removed", "role_changed"]
    details: str
    source_url: str
    source_page: int | None = Field(default=None, ge=1)


class ShareholderChangeEvent(BaseModel):
    effective_date: date
    shareholder_name: str
    details: str
    source_url: str
    source_page: int | None = Field(default=None, ge=1)


class Shareholder(BaseModel):
    rank: int = Field(ge=1, le=10)
    name: str = Field(min_length=1)
    shareholding_percent: float = Field(ge=0, le=100)
    insider_status: DisclosureBool = NOT_DISCLOSED
    pledged_status: DisclosureBool = NOT_DISCLOSED
    report_period: str = Field(min_length=1)
    source_page: int = Field(ge=1)


class ShareholderSnapshot(BaseModel):
    report_period: str = Field(min_length=1)
    reporting_basis: str = Field(min_length=1)
    shareholders: list[Shareholder] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_same_period_and_ranks(self) -> "ShareholderSnapshot":
        if any(item.report_period != self.report_period for item in self.shareholders):
            raise ValueError("all top-ten shareholders must use the snapshot report period")
        ranks = [item.rank for item in self.shareholders]
        if len(ranks) != len(set(ranks)):
            raise ValueError("shareholder ranks must be unique")
        return self


class EmployeeBreakdown(BaseModel):
    prc: int | None = Field(default=None, ge=0)
    us: int | None = Field(default=None, ge=0)
    europe: int | None = Field(default=None, ge=0)
    other: int | None = Field(default=None, ge=0)
    total: int = Field(ge=0)
    source_page: int = Field(ge=1)

    @property
    def disclosed_region_sum(self) -> int:
        return sum(value for value in (self.prc, self.us, self.europe, self.other) if value is not None)

    @property
    def discrepancy(self) -> int | None:
        if any(value is None for value in (self.prc, self.us, self.europe, self.other)):
            return None
        return self.total - self.disclosed_region_sum


class ControllerChange(BaseModel):
    changed: bool
    details: str | None = None
    source: Literal["annual_report", "interim_report", "cninfo", "exchange"]
    source_url: str
    evidence_date: date
    source_page: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_change_details(self) -> "ControllerChange":
        if self.changed and not self.details:
            raise ValueError("controller change details are required when changed=true")
        return self


class GovernanceReportInput(BaseModel):
    report_period: str = Field(min_length=1)
    report_type: Literal["annual_report", "interim_report"]
    source_url: str
    captured_at: datetime
    report_date: date
    persons: list[Person]
    board_structure: BoardStructure
    current_shareholders: ShareholderSnapshot
    previous_shareholders: ShareholderSnapshot | None = None
    post_report_changes: list[GovernanceChange] = Field(default_factory=list)
    post_report_shareholder_changes: list[ShareholderChangeEvent] = Field(default_factory=list)
    controller_change: ControllerChange
    employees: EmployeeBreakdown

    @model_validator(mode="after")
    def validate_current_snapshot(self) -> "GovernanceReportInput":
        if self.current_shareholders.report_period != self.report_period:
            raise ValueError("current shareholders must match governance report period")
        return self


class ShareholderChangeResult(BaseModel):
    status: Literal["Yes", "No", "Not disclosed"]
    details: list[str] = Field(default_factory=list)


class GovernanceResult(BaseModel):
    persons: list[Person]
    current_directors: list[Person]
    directors: list[Person]
    executives: list[Person]
    supervisors: list[Person]
    board_structure: BoardStructure
    director_officer_changes: list[GovernanceChange]
    major_shareholders: list[Shareholder]
    shareholder_changes: ShareholderChangeResult
    post_report_shareholder_changes: list[ShareholderChangeEvent]
    controller_change: ControllerChange
    employees: EmployeeBreakdown
    employee_discrepancy: int | None


class GovernanceCollection(BaseModel):
    result: GovernanceResult
    source_values: list[SourceValue]


def deduplicate_person_records(persons: list[Person]) -> list[Person]:
    """Remove only exact repeated report records; never merge same-name people by inference."""
    seen: set[tuple[str, tuple[tuple[str, str, bool], ...]]] = set()
    output: list[Person] = []
    for person in persons:
        roles = tuple(sorted((role.category, role.title, role.current) for role in person.roles))
        key = (person.name.strip(), roles)
        if key not in seen:
            seen.add(key)
            output.append(person)
    return output


def classify_people(persons: list[Person]) -> tuple[list[Person], list[Person], list[Person]]:
    directors = [person for person in persons if any(role.category == "director" for role in person.roles)]
    executives = [person for person in persons if any(role.category == "executive" for role in person.roles)]
    supervisors = [person for person in persons if any(role.category == "supervisor" for role in person.roles)]
    return directors, executives, supervisors


def current_directors(persons: list[Person]) -> list[Person]:
    return [
        person
        for person in persons
        if any(role.category == "director" and role.current for role in person.roles)
    ]


def compare_shareholder_snapshots(
    current: ShareholderSnapshot,
    previous: ShareholderSnapshot | None,
) -> ShareholderChangeResult:
    if previous is None or previous.reporting_basis != current.reporting_basis:
        return ShareholderChangeResult(status=NOT_DISCLOSED)
    previous_by_name = {item.name: item for item in previous.shareholders}
    current_by_name = {item.name: item for item in current.shareholders}
    details: list[str] = []
    for name in sorted(current_by_name.keys() | previous_by_name.keys()):
        before = previous_by_name.get(name)
        after = current_by_name.get(name)
        if before is None:
            details.append(f"{name} entered the top ten at rank {after.rank} ({after.shareholding_percent:.2f}%).")
        elif after is None:
            details.append(f"{name} left the top ten (previously {before.shareholding_percent:.2f}%).")
        elif before.rank != after.rank or before.shareholding_percent != after.shareholding_percent:
            details.append(
                f"{name}: rank {before.rank} to {after.rank}; "
                f"shareholding {before.shareholding_percent:.2f}% to {after.shareholding_percent:.2f}%."
            )
    return ShareholderChangeResult(status="Yes" if details else "No", details=details)


def _yn(value: DisclosureBool) -> str:
    if value == NOT_DISCLOSED:
        return NOT_DISCLOSED
    return "Yes" if value else "No"


def _format_profiles(persons: list[Person]) -> str:
    target_titles = {
        "chairman",
        "ceo",
        "chief executive officer",
        "cfo",
        "chief financial officer",
        "company secretary",
        "董事长",
        "总经理",
        "总裁",
        "首席执行官",
        "财务负责人",
        "财务总监",
        "董事会秘书",
        "董秘",
    }
    selected = [
        person
        for person in persons
        if any(role.title.strip().lower() in target_titles and role.current for role in person.roles)
    ]
    lines = []
    for person in selected:
        roles = ", ".join(role.title for role in person.roles if role.current)
        details = "; ".join(
            value
            for value in (
                f"Education: {person.education}" if person.education else None,
                f"Qualifications: {person.qualifications}" if person.qualifications else None,
                f"Experience: {person.work_experience}" if person.work_experience else None,
            )
            if value
        )
        lines.append(f"{person.name} - {roles}. {details or NOT_DISCLOSED}")
    return "\n".join(lines) if lines else NOT_DISCLOSED


def _format_board(board: BoardStructure) -> str:
    return (
        f"Directors: {board.director_total}; Executive Directors: {board.executive_directors}; "
        f"Independent Directors: {board.independent_directors}; Supervisory Board: {_yn(board.supervisory_board)}; "
        f"Audit Committee: {_yn(board.audit_committee)}; Compensation Committee: {_yn(board.compensation_committee)}."
    )


def _format_changes(changes: list[GovernanceChange]) -> str:
    if not changes:
        return "No disclosed changes."
    return "\n".join(
        f"{item.effective_date.isoformat()} - {item.person_name}, {item.role}: {item.change}; {item.details}"
        for item in sorted(changes, key=lambda item: item.effective_date)
    )


def _format_shareholders(shareholders: list[Shareholder]) -> str:
    return "\n".join(
        f"{item.rank}. {item.name} - {item.shareholding_percent:.2f}% - Insider: {_yn(item.insider_status)} "
        f"- Pledged: {_yn(item.pledged_status)}"
        for item in sorted(shareholders, key=lambda item: item.rank)
    )


def collect_governance(data: GovernanceReportInput) -> GovernanceCollection:
    people = deduplicate_person_records(data.persons)
    directors, executives, supervisors = classify_people(people)
    shareholder_changes = compare_shareholder_snapshots(
        data.current_shareholders, data.previous_shareholders
    )
    changes = [
        item
        for item in data.post_report_changes
        if data.report_date < item.effective_date <= data.captured_at.date()
    ]
    shareholder_events = [
        item
        for item in data.post_report_shareholder_changes
        if data.report_date < item.effective_date <= data.captured_at.date()
    ]
    if shareholder_events:
        event_details = [
            f"{item.effective_date.isoformat()} - {item.shareholder_name}: {item.details}"
            for item in sorted(shareholder_events, key=lambda item: item.effective_date)
        ]
        shareholder_changes = ShareholderChangeResult(
            status="Yes",
            details=[*shareholder_changes.details, *event_details],
        )
    result = GovernanceResult(
        persons=people,
        current_directors=current_directors(people),
        directors=directors,
        executives=executives,
        supervisors=supervisors,
        board_structure=data.board_structure,
        director_officer_changes=changes,
        major_shareholders=sorted(data.current_shareholders.shareholders, key=lambda item: item.rank),
        shareholder_changes=shareholder_changes,
        post_report_shareholder_changes=shareholder_events,
        controller_change=data.controller_change,
        employees=data.employees,
        employee_discrepancy=data.employees.discrepancy,
    )
    source: SourceName = data.report_type
    person_evidence = [
        ItemEvidence(
            item_id=person.name,
            source=source,
            source_url=data.source_url,
            period=data.report_period,
            raw_value={"page": person.source_page, "roles": [role.model_dump() for role in person.roles]},
        )
        for person in people
    ]
    shareholder_evidence = [
        ItemEvidence(
            item_id=str(item.rank),
            source=source,
            source_url=data.source_url,
            period=item.report_period,
            raw_value={"page": item.source_page, **item.model_dump()},
        )
        for item in result.major_shareholders
    ]
    governance_change_evidence = [
        ItemEvidence(
            item_id=f"{item.effective_date.isoformat()}:{item.person_name}",
            source="cninfo",
            source_url=item.source_url,
            period=item.effective_date.isoformat(),
            raw_value=item.model_dump(mode="json"),
        )
        for item in changes
    ]
    shareholder_change_evidence = [
        ItemEvidence(
            item_id=f"{item.effective_date.isoformat()}:{item.shareholder_name}",
            source="cninfo",
            source_url=item.source_url,
            period=item.effective_date.isoformat(),
            raw_value=item.model_dump(mode="json"),
        )
        for item in shareholder_events
    ]

    def value(field_id: str, text: str, raw: Any, page: int | None = None, item_evidence=None) -> SourceValue:
        metadata = {"page": page} if page is not None else {}
        return SourceValue(
            field_id=field_id,
            value=text,
            raw_value=raw,
            source=source,
            source_url=data.source_url,
            captured_at=data.captured_at,
            period=data.report_period,
            metadata=metadata,
            item_evidence=item_evidence or [],
        )

    shareholder_detail = "\n".join(shareholder_changes.details) or (
        "No change identified on the same reporting basis."
        if shareholder_changes.status == "No"
        else NOT_DISCLOSED
    )
    values = [
        value("key_executive_profiles", _format_profiles(people), people, item_evidence=person_evidence),
        value("board_structure", _format_board(data.board_structure), data.board_structure, data.board_structure.source_page),
        value(
            "director_officer_changes",
            _format_changes(changes),
            changes,
            item_evidence=governance_change_evidence,
        ),
        value("major_shareholders", _format_shareholders(result.major_shareholders), result.major_shareholders, item_evidence=shareholder_evidence),
        value(
            "major_shareholder_change_status",
            shareholder_changes.status,
            shareholder_changes,
            item_evidence=shareholder_change_evidence,
        ),
        value(
            "major_shareholder_change_details",
            shareholder_detail,
            shareholder_changes,
            item_evidence=shareholder_change_evidence,
        ),
    ]
    for field_id, employee_value in (
        ("employees_prc", data.employees.prc),
        ("employees_us", data.employees.us),
        ("employees_europe", data.employees.europe),
        ("employees_other", data.employees.other),
        ("employees_total", data.employees.total),
    ):
        values.append(
            value(
                field_id,
                NOT_DISCLOSED if employee_value is None else str(employee_value),
                data.employees,
                data.employees.source_page,
            )
        )
    return GovernanceCollection(result=result, source_values=values)


def collect_governance_node(state: WorkupAgentState) -> dict[str, Any]:
    raw = state.get("part_results", {}).get("part_10_input")
    if raw is None:
        return {
            "part_results": {
                **state.get("part_results", {}),
                "part_10": {"status": "failed", "reason": "part_10_input is missing"},
            },
            "errors": [
                *state.get("errors", []),
                {"node": "collect_governance", "message": "part_10_input is missing"},
            ],
        }
    collection = collect_governance(GovernanceReportInput.model_validate(raw))
    return {
        "part_results": {
            **state.get("part_results", {}),
            "part_10": collection.result.model_dump(mode="json"),
        },
        "source_values": [
            *state.get("source_values", []),
            *(item.model_dump(mode="json") for item in collection.source_values),
        ],
    }
