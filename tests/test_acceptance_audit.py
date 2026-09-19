from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit_full_acceptance import audit_run


def _copy_json(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_audit_rejects_partial_run_and_requires_render_inspection(tmp_path, monkeypatch):
    mappings = [{"field_id": f"field_{index}", "write_strategy": "replace_text", "locators": []} for index in range(95)]
    monkeypatch.setattr(
        "scripts.audit_full_acceptance.load_template_mapping",
        lambda _config_dir: {"fields": mappings},
    )
    (tmp_path / "result.docx").write_bytes(b"not a docx")
    _copy_json(tmp_path / "sources.json", {"evidence": [{"field_id": "only_one", "source_url": "https://example.test"}], "conflicts": []})
    _copy_json(tmp_path / "failed_fields.json", [{"field_id": "missing", "reason": "missing"}])
    _copy_json(tmp_path / "extracted_data.json", {"run": {}, "company": {}, "node_errors": []})
    _copy_json(tmp_path / "execution_plan.json", [])

    report = audit_run(tmp_path, render=False, inspection_manifest=None)

    assert report["status"] == "FAIL"
    assert report["checks"]["success_count_is_95"]["passed"] is False
    assert report["checks"]["failed_count_is_0"]["passed"] is False
    assert report["checks"]["all_95_values_match_docx_mapping"]["passed"] is False
    assert report["checks"]["visual_inspection"]["passed"] is False
    assert (tmp_path / "acceptance_report.json").exists()


def test_audit_rejects_wrong_persisted_artifact_container_types(tmp_path, monkeypatch):
    mappings = [{"field_id": f"field_{index}", "write_strategy": "replace_text", "locators": []} for index in range(95)]
    monkeypatch.setattr(
        "scripts.audit_full_acceptance.load_template_mapping",
        lambda _config_dir: {"fields": mappings},
    )
    (tmp_path / "result.docx").write_bytes(b"not a docx")
    _copy_json(tmp_path / "sources.json", {"evidence": [], "conflicts": []})
    _copy_json(tmp_path / "failed_fields.json", {})
    _copy_json(tmp_path / "extracted_data.json", {"run": {}, "company": {}, "node_errors": {}})
    _copy_json(tmp_path / "execution_plan.json", {})

    report = audit_run(tmp_path, render=False, inspection_manifest=None)

    assert report["status"] == "FAIL"
    assert report["checks"]["failed_fields_json_is_list"]["passed"] is False
    assert report["checks"]["extracted_data_node_errors_is_list"]["passed"] is False
    assert report["checks"]["execution_plan_is_list"]["passed"] is False


def test_audit_visual_manifest_requires_every_page_with_notes(tmp_path):
    render_dir = tmp_path / "rendered"
    render_dir.mkdir()
    page = render_dir / "page-1.png"
    page.write_bytes(b"png")

    from scripts.audit_full_acceptance import _visual_check

    result = _visual_check({"pages": [{"page": 1, "bytes": 3}]}, None)
    assert result["passed"] is False

    manifest = tmp_path / "inspection.json"
    manifest.write_text(json.dumps({"pages": [{"page": 1, "status": "pass", "notes": "checked"}]}), encoding="utf-8")
    result = _visual_check({"pages": [{"page": 1, "bytes": 3}]}, manifest)
    assert result["passed"] is True
