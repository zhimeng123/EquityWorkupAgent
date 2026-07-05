from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypedDict

from pydantic import AliasChoices, BaseModel, Field, model_validator


SourceName = Literal[
    "fixed",
    "system",
    "eastmoney",
    "xueqiu",
    "official_site",
    "cninfo",
    "annual_report",
    "interim_report",
    "exchange",
    "market_history",
    "news",
    "derived",
]
WriteStrategy = Literal[
    "replace_text",
    "replace_with_hyperlink",
    "replace_multiline_text",
    "fill_fixed_table",
    "insert_image",
    "append_after_label",
]
OutputFormat = Literal["text", "url", "multiline", "table", "image"]


class TablePathStep(BaseModel):
    table_index: int = Field(ge=0)
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)


class TemplateLocator(BaseModel):
    kind: Literal["paragraph", "header", "table_cell", "image_anchor"]
    paragraph_index: int | None = Field(default=None, ge=0)
    header_type: Literal["default", "first", "even"] = "default"
    table_path: list[TablePathStep] = Field(default_factory=list)
    expected_text: str | None = None
    expected_empty: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_table_coordinates(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "table_path" in value or "table_index" not in value:
            return value
        value = dict(value)
        value["table_path"] = [
            {
                "table_index": value.pop("table_index"),
                "row_index": value.pop("row_index"),
                "column_index": value.pop("column_index"),
            }
        ]
        return value

    @model_validator(mode="after")
    def validate_location(self) -> "TemplateLocator":
        if (self.expected_text is None) == (not self.expected_empty):
            raise ValueError("locator must define exactly one of expected_text or expected_empty=true")
        if self.kind in {"paragraph", "header"}:
            if self.paragraph_index is None or self.table_path:
                raise ValueError(f"{self.kind} locator requires paragraph_index and no table_path")
        else:
            if not self.table_path:
                raise ValueError(f"{self.kind} locator requires table_path")
        return self


class FieldMapping(BaseModel):
    field_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    locators: list[TemplateLocator] = Field(
        min_length=1,
        validation_alias=AliasChoices("locators", "locator"),
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_single_locator(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = dict(value)
            if "locators" not in value and isinstance(value.get("locator"), dict):
                value["locator"] = [value["locator"]]
            if "output_format" not in value:
                value["output_format"] = {
                    "replace_with_hyperlink": "url",
                    "replace_multiline_text": "multiline",
                    "fill_fixed_table": "table",
                    "insert_image": "image",
                }.get(value.get("write_strategy"), "text")
        return value
    write_strategy: WriteStrategy = "replace_text"
    output_format: OutputFormat = "text"
    output_style: str | None = None

    @model_validator(mode="after")
    def validate_strategy_format(self) -> "FieldMapping":
        required = {
            "replace_with_hyperlink": "url",
            "replace_multiline_text": "multiline",
            "fill_fixed_table": "table",
            "insert_image": "image",
        }
        expected = required.get(self.write_strategy)
        if expected is not None and self.output_format != expected:
            raise ValueError(f"{self.write_strategy} requires output_format={expected}")
        return self


class TemplateMappingConfig(BaseModel):
    template_version: str = Field(min_length=1)
    template_filename: str = Field(min_length=1)
    includes: list[str] = Field(default_factory=list)
    fields: list[FieldMapping]


class DerivedInput(BaseModel):
    field_id: str
    value: Any
    period: str | None = None
    source_url: str


class DerivedEvidence(BaseModel):
    formula: str
    inputs: list[DerivedInput] = Field(min_length=1)
    result: Any


class ItemEvidence(BaseModel):
    item_id: str | None = None
    source: SourceName
    source_url: str
    period: str | None = None
    raw_value: Any = None


class CompanyIdentity(BaseModel):
    company_name: str
    company_short_name: str
    company_english_name: str | None = None
    stock_code: str
    exchange: str
    eastmoney_secid: str
    eastmoney_secu_code: str
    xueqiu_symbol: str
    official_website: str | None = None


class SourceValue(BaseModel):
    field_id: str
    value: str
    raw_value: Any = None
    source: SourceName
    source_url: str
    captured_at: datetime
    period: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    derived_evidence: DerivedEvidence | None = None
    item_evidence: list[ItemEvidence] = Field(default_factory=list)


class FieldResult(BaseModel):
    field_id: str
    label: str
    value: str
    selected_source: SourceName
    period: str | None = None
    structured_value: Any = None
    artifact_path: str | None = None


class EvidenceRecord(BaseModel):
    field_id: str
    value: str
    selected_source: SourceName
    source_url: str
    captured_at: datetime
    raw_value: Any = None
    normalized_value: str
    period: str | None = None
    selection_reason: str
    conflict_values: list[dict[str, Any]] = Field(default_factory=list)
    supporting_sources: list[dict[str, str]] = Field(default_factory=list)
    derived_evidence: DerivedEvidence | None = None
    item_evidence: list[ItemEvidence] = Field(default_factory=list)


class FailedField(BaseModel):
    field_id: str
    label: str
    reason: str
    source_attempted: list[str]
    mvp_scope: bool = True
    suggested_manual_action: str = "Manually verify and complete this field."


class ConflictRecord(BaseModel):
    field_id: str
    candidates: list[dict[str, Any]]
    selected_source: SourceName
    selection_reason: str


class ExecutionStep(BaseModel):
    step_id: str
    title: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    detail: str | None = None


class SelfCheckResult(BaseModel):
    passed: bool
    checks: dict[str, bool]
    issues: list[str] = Field(default_factory=list)


class WorkupAgentState(TypedDict, total=False):
    run_id: str
    goal: str
    company_input: str
    template_path: str
    output_root: str
    run_dir: str
    config_dir: str
    auto_confirm: bool
    debug: bool
    keep_intermediate: bool
    created_at: str
    template_version: str
    field_mapping: list[dict[str, Any]]
    fillable_fields: list[str]
    skipped_fields: list[str]
    execution_plan: list[dict[str, Any]]
    current_step: str
    step_results: list[dict[str, Any]]
    confirmed: bool
    company: dict[str, Any]
    eastmoney_data: dict[str, Any]
    xueqiu_data: dict[str, Any]
    official_site_data: dict[str, Any]
    report_catalog: list[dict[str, Any]]
    announcement_catalog: list[dict[str, Any]]
    part_results: dict[str, Any]
    artifacts: list[dict[str, Any]]
    node_errors: list[dict[str, Any]]
    raw_collected_data: dict[str, Any]
    source_values: list[dict[str, Any]]
    field_results: dict[str, dict[str, Any]]
    evidence_records: list[dict[str, Any]]
    conflict_records: list[dict[str, Any]]
    failed_fields: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    output_docx_path: str
    sources_json_path: str
    failed_fields_json_path: str
    extracted_data_json_path: str
    execution_plan_json_path: str
    log_path: str
    self_check_result: dict[str, Any]
