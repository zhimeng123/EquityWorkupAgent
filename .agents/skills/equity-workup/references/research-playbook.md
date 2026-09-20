# Research Playbook

This playbook tells Codex / OpenCode how to run a workup. The host agent plans,
researches, finds gaps, and decides retries; the scripts only do deterministic
mechanical work. The host's built-in web search, download, PDF reading, Word, and
rendering tools are preferred over new code.

## 0. Preconditions

- One target A-share listed company and two competitor A-share companies. By
  default both competitors are supplied by the human; never auto-discover, rank,
  replace, or add peers. Only if the human explicitly authorizes it may the agent
  select the two competitors, in which case `run-plan.json` must record
  `peer_selection.mode = "user_authorized_auto"` with the authorization text and
  a comparability rationale.
- Optional cut-off date; otherwise use the run date.
- Confirm all three resolve to distinct listed companies (name ↔ ticker). If not,
  stop and ask.

## 1. Set up the run

1. Create a run directory (for example `sample-run/<target-ticker>-<date>/`).
2. Write `run-plan.json`: the three confirmed identities (name, ticker,
   exchange), the cut-off date, the field list from `report-contract.md`, the
   intended source order, and the retry plan.
3. Prepare the working template once:

   ```bash
   python .agents/skills/equity-workup/scripts/write_report.py prepare \
     --template "Workup_template_260617-外测版.docx" \
     --out <run-dir>/working-template.docx \
     --manifest <run-dir>/template-manifest.json
   ```

   This copies the original template (leaving it untouched), removes the third
   competitor row, changes the peer-count wording to two, and adds stable
   bookmarks. It records the original hash.

## 2. Collect shared primary documents first

Download once and reuse: latest annual report, latest interim/quarterly report,
the last two years of annual reports, and exchange announcements since the latest
annual report. Extract text with the host's PDF tool. Keep a small index of
`(doc_id, url, title, published_at, local_path)` in the run directory so every
field can cite `page_or_section`.

## 3. Research in field order

Work section by section (company overview → US exposure → operating performance →
liquidity → financial analysis → security exposure → governance → shareholders →
employees → audit → non-financial changes → litigation → news). For each field:

1. Find the value in a primary document; record the verbatim `evidence_quote` and
   `page_or_section`.
2. Write the evidence record immediately. Never batch sources at the end.
3. For ratios and market metrics, do not compute by hand: feed the raw inputs to
   `calculate_metrics.py` and cite its output as `derived`.

Hosts with subagents may parallelise independent sections. Hosts without them run
the same order sequentially. Parallelism must not change the evidence standard.

## 4. Deterministic calculations and charts

Prepare a `metrics-input.json` with the raw financial and market inputs (see the
script's `--help` and the tests for the exact shape), then run:

```bash
python .agents/skills/equity-workup/scripts/calculate_metrics.py \
  --input <run-dir>/metrics-input.json \
  --output <run-dir>/metrics.json \
  --chart-dir <run-dir>
```

Use the returned values verbatim in the evidence records (`status: derived`,
`calculation` naming the formula). The script also writes
`standalone-stock-chart.png` and `competitor-comparison-chart.png`.

## 5. Gap review and bounded retry

After the first pass, list unresolved fields. Retry a field only when a concrete
alternative source or extraction method exists (for example, the announcement
instead of the annual report, or a different section). At most two effective
paths per field; one full retry round with no new resolutions stops the loop.
Write every attempt into `gaps.json`.

## 6. Write and audit

1. Fill the report:

   ```bash
   python .agents/skills/equity-workup/scripts/write_report.py write \
     --working <run-dir>/working-template.docx \
     --evidence <run-dir>/evidence.json \
     --out <run-dir>/result.docx \
     --charts <run-dir>
   ```

   Only qualified fields are written; `unresolved` fields and all manual-only
   regions are left untouched.

2. Audit:

   ```bash
   python .agents/skills/equity-workup/scripts/audit_report.py \
     --run <run-dir> \
     --template "Workup_template_260617-外测版.docx" \
     --render
   ```

3. Render and inspect every page (`renders/page-N.png`). The host must review the
   pages for text clipping, overlap, broken tables, and misplaced images. Fix the
   inputs and re-run if anything is wrong; never ship an unreviewed page.

## 7. Negative-news attachments

When the report lists negative news, create a DOCX attachment per the contract.
Do not copy full text that cannot be lawfully retrieved; use
`Full text unavailable` instead. Never fabricate a body or a summary.

## 8. Reporting back

Summarise: what was produced, which fields are `unresolved` and why, which
retries were attempted, and any residual risk. State explicitly that the report
contains only the target and the two human-named competitors.
