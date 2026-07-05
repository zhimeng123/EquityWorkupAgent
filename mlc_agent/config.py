from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from mlc_agent.exceptions import TemplateMappingError
from mlc_agent.schemas import TemplateMappingConfig


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Configuration must be a mapping: {path}")
    return data


def load_template_mapping(config_dir: Path) -> dict[str, Any]:
    main_path = config_dir / "fixed_template_mapping.yaml"
    data = load_yaml(main_path)
    fields = list(data.get("fields", []))
    includes = data.get("includes", [])
    if not isinstance(includes, list) or not all(isinstance(item, str) for item in includes):
        raise TemplateMappingError("mapping includes must be a list of relative file paths")
    for relative_path in includes:
        include_path = (config_dir / relative_path).resolve()
        try:
            include_path.relative_to(config_dir.resolve())
        except ValueError as exc:
            raise TemplateMappingError(f"mapping include escapes config directory: {relative_path}") from exc
        fragment = load_yaml(include_path)
        fragment_fields = fragment.get("fields")
        if not isinstance(fragment_fields, list):
            raise TemplateMappingError(f"mapping fragment fields must be a list: {relative_path}")
        fields.extend(fragment_fields)
    data["fields"] = fields
    try:
        config = TemplateMappingConfig.model_validate(data)
    except ValidationError as exc:
        raise TemplateMappingError(f"invalid template mapping: {exc}") from exc
    _validate_mapping_uniqueness(config)
    return config.model_dump(mode="json")


def _locator_target(locator: Any) -> tuple[Any, ...]:
    return (
        locator.kind,
        locator.header_type,
        locator.paragraph_index,
        tuple((step.table_index, step.row_index, step.column_index) for step in locator.table_path),
    )


def _validate_mapping_uniqueness(config: TemplateMappingConfig) -> None:
    field_ids: set[str] = set()
    targets: dict[tuple[Any, ...], str] = {}
    for field in config.fields:
        if field.field_id in field_ids:
            raise TemplateMappingError(f"duplicate field_id: {field.field_id}")
        field_ids.add(field.field_id)
        for locator in field.locators:
            target = _locator_target(locator)
            owner = targets.get(target)
            if owner is not None:
                raise TemplateMappingError(
                    f"conflicting locator target used by {owner} and {field.field_id}: {target}"
                )
            targets[target] = field.field_id


def load_source_priorities(
    config_dir: Path,
    mappings: list[dict[str, Any]],
) -> dict[str, list[str]]:
    data = load_yaml(config_dir / "source_priority_policy.yaml")
    field_priorities = data.get("field_priorities", {})
    source_type_priorities = data.get("source_type_priorities", {})
    resolved: dict[str, list[str]] = {}
    missing: list[str] = []
    for mapping in mappings:
        field_id = mapping["field_id"]
        priority = field_priorities.get(field_id) or source_type_priorities.get(
            mapping["source_type"]
        )
        if not isinstance(priority, list) or not priority:
            missing.append(field_id)
        else:
            resolved[field_id] = priority
    if missing:
        raise TemplateMappingError(
            "source priority is missing for mapped fields: " + ", ".join(missing)
        )
    return resolved
