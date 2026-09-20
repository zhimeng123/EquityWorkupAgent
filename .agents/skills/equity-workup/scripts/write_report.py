#!/usr/bin/env python3
"""Prepare a working Word template and write verified fields into it.

Two subcommands:

``prepare``
    Copy the immutable original template, remove the third competitor row,
    change the peer-count wording to two, and insert stable invisible bookmarks
    (``EQW_<field_id>``). Records the original SHA-256 in a manifest.

``write``
    Write qualified fields (``supported`` / ``derived`` / ``not_disclosed`` /
    ``not_applicable``) and the two charts into the working template. Refuses
    manual-only regions and never writes ``unresolved`` fields.

The script never generates research conclusions and never edits the original
template. See ``references/report-contract.md``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches
from docx.text.paragraph import Paragraph

WRITABLE_STATUSES = {"supported", "derived", "not_disclosed", "not_applicable"}
STATUS_TEXT = {"not_disclosed": "Not disclosed", "not_applicable": "Not applicable"}
CHART_WIDTH_INCHES = 6.3
PEER_NOTE_FROM = "选取3家"
PEER_NOTE_TO = "选取2家"

# Manual-only field ids. The writer refuses these outright; the registry below
# contains no slot for them, so they cannot be written even by accident.
MANUAL_FIELD_IDS = {
    "manual.underwriter", "manual.branch", "manual.producer", "manual.commission",
    "manual.reason_for_referral", "manual.date_approval_required",
    "manual.overall_relationship", "manual.brief_of_competition",
    "manual.clearance_obtained", "manual.clearance_who",
    "manual.nb_or_renewal", "manual.written_since", "manual.premium_earned",
    "manual.claim_history", "manual.rationale_for_recommendation",
    "manual.recommended_dno", "manual.recommended_posi", "manual.subjectivities",
    "manual.rated_premium", "manual.rating_rationale",
    "manual.indicated_premium_explanation", "manual.sign_off", "manual.date",
}

# --------------------------------------------------------------------------- #
# Field registry: field_id -> slot locator. keep_prefix is template text kept in
# front of the value. Locators are resolved at prepare time only; the resulting
# bookmarks are what write mode depends on.
# --------------------------------------------------------------------------- #
MA_PAST_PREFIX = (
    "Any major M&A during the past 12 months? If so, please provide full "
    "details (including date, name, country, listed or not, consideration "
    "and comments): "
)
MA_PLAN_PREFIX = (
    "Any M&A plan in the next 12 months? If so, please provide full "
    "details (including date, name, country, listed or not, consideration "
    "and comments): "
)
REVENUE_BREAKDOWN_PREFIX = "Revenue Breakdown By Business Segment & Geography: "
DETAIL_PREFIX = "If yes to the above, please provide full detail: "
BOARD_PREFIX = (
    "Structure of the Board (number of Executive Directors and Independent "
    "Directors), whether there is a Supervisory Board, Audit Committee, "
    "Compensation Committee: "
)
DIRECTOR_PREFIX = (
    "Whether there is any change to directors or officers in the past year? "
    "If so, please provide full detail: "
)


def _cell(field_id, table, row, label=None, col=None, value_offset=1, keep_prefix=""):
    entry = {"id": field_id, "table": table, "row": row}
    if label is not None:
        entry["label"] = label
        entry["value_offset"] = value_offset
    if col is not None:
        entry["col"] = col
    if keep_prefix:
        entry["keep_prefix"] = keep_prefix
    return entry


def _para(field_id, para_prefix, keep_prefix=""):
    entry = {"id": field_id, "para_prefix": para_prefix}
    if keep_prefix:
        entry["keep_prefix"] = keep_prefix
    return entry


REGISTRY: list[dict[str, Any]] = [
    # Company overview -------------------------------------------------------
    _cell("co.country_of_incorp", 2, 0, "Country of Incorp.:"),
    _cell("co.soe_status", 2, 0, "SOE / Non-SOE:"),
    _cell("co.total_asset", 2, 1, "Total Asset:"),
    _cell("co.total_equity", 2, 1, "Total Equity"),
    _cell("co.annual_revenue", 2, 2, "Annual Revenue:"),
    _cell("co.annual_net_profit", 2, 2, "Annual Net Profit:"),
    _cell("co.listed_or_private", 2, 3, "Is the Proposer Listed", value_offset=2),
    _cell("co.listed_exchange", 2, 4, "Listed Exchange:"),
    _cell("co.market_cap", 2, 4, "Market Capitalization:"),
    _cell("co.listed_subsidiary", 2, 5,
          "Does the Proposer has any listed Subsidiary:"),
    _cell("co.listed_outside_directorship", 2, 6,
          "Does the Proposer has any listed Outside Directorship"),
    _cell("co.ma_past_12m", 2, 7, col=0, keep_prefix=MA_PAST_PREFIX),
    _para("co.ma_plan_next_12m", "Any M&A plan in the next 12 months?",
          MA_PLAN_PREFIX),
    _para("co.website", "Official website link:", "Official website link: "),
    _para("co.google_finance", "Google finance link:", "Google finance link: "),
    _para("co.cninfo", "巨潮资讯网链接：", "巨潮资讯网链接："),
    _para("co.xueqiu", "雪球网链接：", "雪球网链接："),
    _para("co.eastmoney", "东方财富网链接：", "东方财富网链接："),
    _para("co.business_description", "可在最新年报/半年报上抓取"),
    # US exposure ------------------------------------------------------------
    _cell("us.subsidiaries", 3, 0, "US Subsidiaries:"),
    _cell("us.num_subsidiaries", 3, 0, "# of US Subsidiary:"),
    _cell("us.other_details", 3, 1, "Other Details:"),
    _cell("us.revenue", 3, 2, "US Revenue:"),
    _cell("us.revenue_pct", 3, 2, "% of Total Revenue:"),
    _cell("us.details_of_sub", 3, 3, "Details of US Sub.:"),
    _cell("us.employees", 3, 4, "US Employees:"),
    _cell("us.split_by_state", 3, 5, "Split by State:"),
    # Operating performance --------------------------------------------------
    _cell("op.revenue_breakdown", 4, 0, col=0,
          keep_prefix=REVENUE_BREAKDOWN_PREFIX),
    _cell("op.revenue_trend", 5, 0, col=0),
    _cell("op.outlook_risks", 6, 0, col=0),
    _cell("op.related_party", 7, 0, col=0),
    _cell("op.peer_confirmation", 8, 0, "Confirmation that Revenue Trend"),
    {"id": "op.peer.proposer.name", "peer_cell": [1, 0]},
    {"id": "op.peer.proposer.revenue", "peer_cell": [1, 1]},
    {"id": "op.peer.proposer.inventory_turnover", "peer_cell": [1, 2]},
    {"id": "op.peer.proposer.gross_margin", "peer_cell": [1, 3]},
    {"id": "op.peer.proposer.net_margin", "peer_cell": [1, 4]},
    {"id": "op.peer1.name", "peer_cell": [2, 0]},
    {"id": "op.peer1.revenue", "peer_cell": [2, 1]},
    {"id": "op.peer1.inventory_turnover", "peer_cell": [2, 2]},
    {"id": "op.peer1.gross_margin", "peer_cell": [2, 3]},
    {"id": "op.peer1.net_margin", "peer_cell": [2, 4]},
    {"id": "op.peer2.name", "peer_cell": [3, 0]},
    {"id": "op.peer2.revenue", "peer_cell": [3, 1]},
    {"id": "op.peer2.inventory_turnover", "peer_cell": [3, 2]},
    {"id": "op.peer2.gross_margin", "peer_cell": [3, 3]},
    {"id": "op.peer2.net_margin", "peer_cell": [3, 4]},
    # Liquidity & debt -------------------------------------------------------
    _cell("liq.current_ratio_prev", 9, 1, "Current Ratio:"),
    _cell("liq.current_ratio_curr", 9, 1, "Current Ratio:", value_offset=2),
    _cell("liq.quick_ratio_prev", 9, 1, "Quick Ratio:"),
    _cell("liq.quick_ratio_curr", 9, 1, "Quick Ratio:", value_offset=2),
    _cell("liq.capex_prev", 9, 2, "CAPEX:"),
    _cell("liq.capex_curr", 9, 2, "CAPEX:", value_offset=2),
    _cell("liq.capex_to_revenue_prev", 9, 2, "CAPEX to Revenue:"),
    _cell("liq.capex_to_revenue_curr", 9, 2, "CAPEX to Revenue:", value_offset=2),
    _cell("liq.interest_coverage_prev", 9, 3, "Interest Coverage Ratio:"),
    _cell("liq.interest_coverage_curr", 9, 3, "Interest Coverage Ratio:",
          value_offset=2),
    _cell("liq.debt_to_asset_prev", 9, 3, "Debt to Asset Ratio:"),
    _cell("liq.debt_to_asset_curr", 9, 3, "Debt to Asset Ratio:", value_offset=2),
    _cell("liq.ocf_positive_prev", 9, 4, "OCF Positive (Y/N):"),
    _cell("liq.ocf_positive_curr", 9, 4, "OCF Positive (Y/N):", value_offset=2),
    _cell("liq.cashflow_gt_noi_prev", 9, 4, "Cash Flow > NOI (Y/N):"),
    _cell("liq.cashflow_gt_noi_curr", 9, 4, "Cash Flow > NOI (Y/N):",
          value_offset=2),
    _cell("liq.short_term_debt_concern", 9, 6,
          "Are there significant amount of short term debt"),
    _cell("liq.positive_ocf", 9, 7,
          "Is the Company generating positive operation cash flow?"),
    _cell("liq.net_income_exceed_ocf", 9, 8,
          "Does net income exceed cash flow from operating activities?"),
    _cell("liq.comments", 9, 10, col=0),
    # Financial analysis -----------------------------------------------------
    _cell("fin.ar_growth_vs_revenue", 10, 0,
          "Is account receivable growth higher"),
    _cell("fin.one_time_writedown", 10, 1,
          "Has the company taken a significant one-time write-down"),
    _cell("fin.intangibles_gt_25pct", 10, 2,
          "Do intangibles represent more than 25%"),
    # Security exposure ------------------------------------------------------
    _cell("sec.ipo_date", 11, 0, "IPO Date:"),
    _cell("sec.total_market_cap", 11, 0, "Total Market Cap:"),
    _cell("sec.current_price", 11, 1, "Current Price:"),
    _cell("sec.52w_low", 11, 2, "52 Week Low:"),
    _cell("sec.52w_high", 11, 2, "52 Week High:"),
    _cell("sec.align_index", 11, 3, "Align to Index:"),
    _cell("sec.align_peers", 11, 3, "Align to Peers:"),
    _cell("sec.significant_drop", 12, 0,
          "Was there a specific & significant stock drop"),
    _cell("sec.drop_details", 12, 1, col=0, keep_prefix=DETAIL_PREFIX),
    _cell("sec.securities_offerings", 12, 2,
          "Has the Company made any securities offerings"),
    _cell("sec.offerings_details", 12, 3, DETAIL_PREFIX.rstrip()),
    _para("sec.charts", "可在雪球网上抓取股价走势图"),
    # Corporate governance ---------------------------------------------------
    _cell("cg.management_highlights", 14, 1, col=0),
    _cell("cg.board_structure", 15, 0, col=0, keep_prefix=BOARD_PREFIX),
    _cell("cg.director_changes", 15, 1, col=0, keep_prefix=DIRECTOR_PREFIX),
    # Shareholders & employees ----------------------------------------------
    {"id": "sh.major_shareholders", "cell_nested_para": [16, 0, 0]},
    _cell("sh.major_change_12m", 16, 1,
          "Has there been any major change in substantial shareholders"),
    _cell("sh.change_details", 16, 2, DETAIL_PREFIX.rstrip()),
    {"id": "emp.headcount", "emp_row": [17, 1]},
    # Audit ------------------------------------------------------------------
    _cell("au.auditor", 18, 0, "Auditor; If non-BIG 4"),
    _cell("au.opinion", 18, 1, "Opinion:"),
    _cell("au.qualified_details", 18, 2, "If “Qualified”"),
    _cell("au.auditor_change", 18, 3,
          "Has the Proposer changed its auditors in the past 2 years:"),
    _cell("au.auditor_change_details", 18, 4, DETAIL_PREFIX.rstrip()),
    _cell("au.restatement", 18, 5, "Has the Proposer restated their financial"),
    _cell("au.restatement_details", 18, 6, DETAIL_PREFIX.rstrip()),
    # Non-financial changes --------------------------------------------------
    _cell("nf.board_change", 19, 0, "Major changes in board structure"),
    _cell("nf.business_change", 19, 1, "Material change in business operations?"),
    _cell("nf.top3_shareholder_change", 19, 2, "Changes to top 3 shareholders?"),
    _cell("nf.details", 19, 3,
          "If yes to any of the above, please provide full details:"),
    # Litigation & news ------------------------------------------------------
    _cell("lit.pending", 20, 0, "Are there any pending & prior litigations"),
    _cell("lit.regulatory", 20, 1, "Has there been any regulatory"),
    _cell("lit.details", 20, 2, DETAIL_PREFIX.rstrip()),
    _cell("news.negative_news", 21, 0, col=0),
]

REGISTRY_BY_ID = {entry["id"]: entry for entry in REGISTRY}
PEER_TABLE_PARENT = [8, 1, 0]  # top-level table 8, row 1, cell 0


def bookmark_name(field_id: str) -> str:
    return "EQW_" + field_id.replace(".", "_").replace("-", "_")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #
def _iter_paragraphs(element) -> list[Paragraph]:
    return [Paragraph(p, None) for p in element.iter(qn("w:p"))]


def _distinct_cells(row) -> list:
    cells = []
    seen = None
    for cell in row.cells:
        if cell._tc is seen:
            continue
        seen = cell._tc
        cells.append(cell)
    return cells


def _find_paragraph_by_prefix(doc: Document, prefix: str) -> Paragraph:
    matches = [p for p in doc.paragraphs if p.text.strip().startswith(prefix)]
    if len(matches) != 1:
        raise LookupError(
            f"expected exactly one paragraph starting with {prefix!r}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _resolve_cell(doc: Document, entry: dict[str, Any]):
    table = doc.tables[entry["table"] - 1]
    row = table.rows[entry["row"]]
    cells = _distinct_cells(row)
    if "col" in entry:
        return cells[entry["col"]]
    label = entry["label"]
    index = None
    for i, cell in enumerate(cells):
        if cell.text.strip().startswith(label):
            index = i
            break
    if index is None:
        raise LookupError(f"{entry['id']}: label {label!r} not found in table {entry['table']}")
    return cells[index + entry.get("value_offset", 1)]


def _peer_table(doc: Document):
    parent = doc.tables[PEER_TABLE_PARENT[0] - 1]
    row = parent.rows[PEER_TABLE_PARENT[1]]
    cell = _distinct_cells(row)[PEER_TABLE_PARENT[2]]
    if not cell.tables:
        raise LookupError("nested peer comparison table not found")
    return cell.tables[0]


def _slot_paragraph(doc: Document, entry: dict[str, Any]) -> Paragraph:
    if "peer_cell" in entry:
        row_idx, col_idx = entry["peer_cell"]
        cell = _distinct_cells(_peer_table(doc).rows[row_idx])[col_idx]
        return cell.paragraphs[0]
    if "emp_row" in entry:
        table_idx, row_idx = entry["emp_row"]
        cell = _distinct_cells(doc.tables[table_idx - 1].rows[row_idx])[1]
        return cell.paragraphs[0]
    if "cell_nested_para" in entry:
        table_idx, row_idx, col_idx = entry["cell_nested_para"]
        cell = _distinct_cells(doc.tables[table_idx - 1].rows[row_idx])[col_idx]
        if not cell.tables:
            raise LookupError(f"{entry['id']}: nested table missing")
        return cell.tables[0].rows[0].cells[0].paragraphs[0]
    if "para_prefix" in entry:
        return _find_paragraph_by_prefix(doc, entry["para_prefix"])
    cell = _resolve_cell(doc, entry)
    return cell.paragraphs[0]


def _insert_bookmark(paragraph: Paragraph, name: str, bookmark_id: int) -> None:
    p = paragraph._p
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    insert_at = 1 if p.find(qn("w:pPr")) is not None else 0
    p.insert(insert_at, start)
    p.append(end)


def _iter_tables(tables):
    for table in tables:
        yield table
        for row in table.rows:
            for cell in row.cells:
                yield from _iter_tables(cell.tables)


def _all_paragraphs(doc: Document):
    yield from doc.paragraphs
    for table in _iter_tables(doc.tables):
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


def _find_bookmark_paragraph(doc: Document, name: str) -> Paragraph | None:
    for paragraph in _all_paragraphs(doc):
        for start in paragraph._p.findall(qn("w:bookmarkStart")):
            if start.get(qn("w:name")) == name:
                return paragraph
    return None


def _first_run_properties(paragraph: Paragraph):
    for child in paragraph._p:
        if child.tag == qn("w:r"):
            rpr = child.find(qn("w:rPr"))
            if rpr is not None:
                return copy.deepcopy(rpr)
    return None


def _clear_paragraph(paragraph: Paragraph):
    rpr = _first_run_properties(paragraph)
    for child in list(paragraph._p):
        if child.tag == qn("w:pPr"):
            continue
        paragraph._p.remove(child)
    return rpr


def _append_text(paragraph: Paragraph, text: str, rpr) -> None:
    lines = str(text).split("\n")
    for i, line in enumerate(lines):
        run = OxmlElement("w:r")
        if rpr is not None:
            run.append(copy.deepcopy(rpr))
        if i:
            run.append(OxmlElement("w:br"))
        t = OxmlElement("w:t")
        t.set(qn("xml:space"), "preserve")
        t.text = line
        run.append(t)
        paragraph._p.append(run)


def _write_slot(paragraph: Paragraph, keep_prefix: str, value: str) -> None:
    rpr = _clear_paragraph(paragraph)
    _append_text(paragraph, f"{keep_prefix}{value}", rpr)


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #
def _replace_peer_count_text(doc: Document) -> int:
    changed = 0
    for p in doc.element.body.iter(qn("w:p")):
        text = "".join(t.text or "" for t in p.iter(qn("w:t")))
        if PEER_NOTE_FROM not in text:
            continue
        paragraph = Paragraph(p, None)
        rpr = _clear_paragraph(paragraph)
        _append_text(paragraph, text.replace(PEER_NOTE_FROM, PEER_NOTE_TO), rpr)
        changed += 1
    return changed


def _remove_third_peer_row(doc: Document) -> None:
    peer_table = _peer_table(doc)
    target = None
    for tr in peer_table._tbl.findall(qn("w:tr")):
        text = "".join(t.text or "" for t in tr.iter(qn("w:t")))
        if "peer 3" in text:
            target = tr
            break
    if target is None:
        raise LookupError("third competitor row not found in peer table")
    peer_table._tbl.remove(target)


def prepare(template: str, out: str, manifest_path: str,
            expected_sha256: str | None = None) -> dict[str, Any]:
    template_path = Path(template)
    out_path = Path(out)
    original_sha = sha256_file(template_path)
    if expected_sha256 and original_sha != expected_sha256:
        raise ValueError(f"original template hash mismatch: {original_sha} != {expected_sha256}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_path, out_path)

    doc = Document(str(out_path))
    _remove_third_peer_row(doc)
    changed = _replace_peer_count_text(doc)

    bookmark_id = 1
    bookmarks: dict[str, Any] = {}
    for entry in REGISTRY:
        paragraph = _slot_paragraph(doc, entry)
        name = bookmark_name(entry["id"])
        _insert_bookmark(paragraph, name, bookmark_id)
        bookmark_id += 1
        bookmarks[entry["id"]] = {
            "bookmark": name,
            "keep_prefix": entry.get("keep_prefix", ""),
        }
    doc.save(str(out_path))

    manifest = {
        "original_path": str(template_path),
        "original_sha256": original_sha,
        "working_path": str(out_path),
        "working_sha256": sha256_file(out_path),
        "prepared_at": datetime.now(UTC).isoformat(),
        "peer_rows_after_prepare": 2,
        "peer_note_replacements": changed,
        "bookmarks": bookmarks,
        "manual_field_ids": sorted(MANUAL_FIELD_IDS),
    }
    Path(manifest_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
def _value_for(record: dict[str, Any]) -> str:
    status = record.get("status")
    if status in STATUS_TEXT and not record.get("value"):
        return STATUS_TEXT[status]
    return str(record.get("value", ""))


def write(working: str, evidence: str, out: str, charts: str | None = None,
          chart_field: str = "sec.charts") -> dict[str, Any]:
    working_path = Path(working)
    with open(evidence, encoding="utf-8") as fh:
        payload = json.load(fh)
    fields = payload.get("fields", payload)

    for field_id in fields:
        if field_id in MANUAL_FIELD_IDS:
            raise ValueError(f"refusing to write manual-only field: {field_id}")
        if field_id not in REGISTRY_BY_ID:
            raise ValueError(f"unknown field id (not in contract): {field_id}")

    doc = Document(str(working_path))
    written: list[str] = []
    skipped: list[str] = []
    for field_id, record in fields.items():
        status = record.get("status")
        if status not in WRITABLE_STATUSES:
            skipped.append(field_id)
            continue
        if field_id == chart_field:
            continue
        entry = REGISTRY_BY_ID[field_id]
        if field_id == "emp.headcount":
            _write_emp(doc, record)
            written.append(field_id)
            continue
        name = bookmark_name(field_id)
        paragraph = _find_bookmark_paragraph(doc, name)
        if paragraph is None:
            raise LookupError(f"{field_id}: bookmark {name} not found in working template")
        _write_slot(paragraph, entry.get("keep_prefix", ""), _value_for(record))
        written.append(field_id)

    charts_written: list[str] = []
    chart_record = fields.get(chart_field)
    if chart_record and chart_record.get("status") in WRITABLE_STATUSES:
        if not charts:
            raise ValueError("chart field is writable but --charts was not provided")
        charts_written = _write_charts(doc, Path(charts), chart_field)

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return {"written": written, "skipped": skipped, "charts": charts_written,
            "output": str(out)}


def _write_emp(doc: Document, record: dict[str, Any]) -> None:
    value = record.get("value") or {}
    if not isinstance(value, dict):
        raise ValueError("emp.headcount value must be an object")
    table = doc.tables[17 - 1]
    cells = _distinct_cells(table.rows[1])
    for key, col in (("prc", 1), ("usa", 2), ("europe", 3), ("row", 4), ("total", 5)):
        if key in value and value[key] is not None:
            _write_slot(cells[col].paragraphs[0], "", str(value[key]))


def _write_charts(doc: Document, charts_dir: Path, chart_field: str) -> list[str]:
    standalone = charts_dir / "standalone-stock-chart.png"
    comparison = charts_dir / "competitor-comparison-chart.png"
    missing = [p.name for p in (standalone, comparison) if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing chart files: {', '.join(missing)}")
    name = bookmark_name(chart_field)
    paragraph = _find_bookmark_paragraph(doc, name)
    if paragraph is None:
        raise LookupError(f"{chart_field}: bookmark {name} not found")
    _clear_paragraph(paragraph)
    paragraph.add_run().add_picture(str(standalone), width=Inches(CHART_WIDTH_INCHES))
    caption1 = _insert_paragraph_after(paragraph, "Figure 1. Target company 2-year adjusted price")
    chart2 = _insert_paragraph_after(caption1, "")
    chart2.add_run().add_picture(str(comparison), width=Inches(CHART_WIDTH_INCHES))
    _insert_paragraph_after(chart2, "Figure 2. Target and two competitors, normalised to 100")
    return [str(standalone), str(comparison)]


def _insert_paragraph_after(paragraph: Paragraph, text: str) -> Paragraph:
    new_p = OxmlElement("w:p")
    paragraph._p.addnext(new_p)
    new_para = Paragraph(new_p, paragraph._parent)
    if text:
        new_para.add_run(text)
    return new_para


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare", help="create the working template copy")
    p_prepare.add_argument("--template", required=True)
    p_prepare.add_argument("--out", required=True)
    p_prepare.add_argument("--manifest", required=True)
    p_prepare.add_argument("--expected-sha256")

    p_write = sub.add_parser("write", help="write verified fields into the report")
    p_write.add_argument("--working", required=True)
    p_write.add_argument("--evidence", required=True)
    p_write.add_argument("--out", required=True)
    p_write.add_argument("--charts")
    p_write.add_argument("--chart-field", default="sec.charts")

    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(args.template, args.out, args.manifest,
                         expected_sha256=args.expected_sha256)
        print(json.dumps({"working": result["working_path"],
                          "original_sha256": result["original_sha256"],
                          "bookmarks": len(result["bookmarks"])}, ensure_ascii=False))
    else:
        result = write(args.working, args.evidence, args.out, charts=args.charts,
                       chart_field=args.chart_field)
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
