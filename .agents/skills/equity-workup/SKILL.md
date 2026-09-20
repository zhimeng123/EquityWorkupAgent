---
name: equity-workup
description: >-
  Produce an evidence-traced Word workup report on one target A-share listed
  company and exactly two human-named A-share competitors, using a fixed DOCX
  template. Use when the user asks for an equity workup, a company due-diligence
  report, or an underwriting-style workup on Chinese A-share companies. The
  skill resolves only the three user-supplied companies, gathers public evidence,
  performs deterministic financial/market calculations, writes the report, and
  audits it. It never discovers or replaces competitors and never fills internal
  underwriting, recommendation, pricing, or sign-off fields.
---

# Equity Workup

Generate a traceable Word workup report for **one target A-share listed company
and exactly two competitor A-share companies chosen by the user**. The host agent
(Codex / OpenCode) does the planning, research, gap checking, and retries. The
scripts only perform deterministic, repeatable mechanical work.

## Read first

- `references/report-contract.md` — field registry, template slots, evidence
  schema, output artifacts, formatting.
- `references/evidence-policy.md` — what counts as evidence, statuses, fixed
  business conventions.
- `references/research-playbook.md` — the end-to-end run procedure.

## Hard rules

1. **Exactly three inputs.** One target + two competitors, all A-share listed, all
   distinct. By default the two competitors must be named by the user: never
   discover, recommend, rank, replace, or add one, and never use a fallback peer
   list. **Guarded exception:** if the user *explicitly authorizes the agent to
   select the two competitors*, record that in `run-plan.json` as
   `peer_selection = {"mode": "user_authorized_auto", "authorization": "<the
   user's words>", "rationale": "<why these two>"}`; both competitors must still
   be A-share listed, distinct from the target, and evidence-justified. Without
   that recorded authorization the default prohibition applies. If an input is
   missing or ambiguous, ask the user.
2. **Evidence or nothing.** Write a field only when it is `supported`, `derived`,
   `not_disclosed`, or `not_applicable`. Everything else is `unresolved` and goes
   to `gaps.json`. An empty search is not `No`. LLM output is never a fact source.
3. **Manual regions stay manual.** Never fill `Underwriter`, `Branch`, `Producer`,
   `Commission`, referral/relationship fields, `Brief of Competition`,
   `Clearance Obtained`, `NB or Renewal`, `Written since`, `Premium Earned`,
   `Claim History`, `Recommendation`, `Recommended D&O`, `Recommended POSI`,
   `Subjectivities`, `Rated Premium`, rating rationale, `Sign-off`, or `Date`.
4. **Do not rebuild host capabilities.** Use the host's web search, download,
   PDF reading, Word handling, and rendering. The scripts below are the only new
   code.
5. **Do not build agent frameworks.** No new planner, LangGraph, scheduler, task
   queue, or multi-agent runtime. The host plans.
6. **The original template is immutable.** Work on a copy; its SHA-256 must be
   unchanged at the end.

## Workflow

1. Confirm the three companies and the cut-off date; write `run-plan.json`.
2. `write_report.py prepare` — copy the template, change three peers to the two
   named competitors, add stable bookmarks, record the original hash.
3. Research shared primary documents once, then fields in contract order, saving
   evidence per field.
4. `calculate_metrics.py` — deterministic ratios, growth, returns, drawdown,
   peer medians, and the two charts.
5. Review unresolved fields; retry only with a concrete alternative path (max two
   paths per field, one full no-progress round stops the loop); write `gaps.json`.
6. `write_report.py write` — fill qualified fields and charts into `result.docx`.
7. `audit_report.py --render` — verify evidence, identity, manual regions,
   template hash, artifacts, and page layout; review every rendered page.

## Scripts

Run from the repository root:

```bash
python .agents/skills/equity-workup/scripts/write_report.py prepare \
  --template "Workup_template_260617-外测版.docx" \
  --out <run-dir>/working-template.docx \
  --manifest <run-dir>/template-manifest.json

python .agents/skills/equity-workup/scripts/calculate_metrics.py \
  --input <run-dir>/metrics-input.json \
  --output <run-dir>/metrics.json \
  --chart-dir <run-dir>

python .agents/skills/equity-workup/scripts/write_report.py write \
  --working <run-dir>/working-template.docx \
  --evidence <run-dir>/evidence.json \
  --metrics <run-dir>/metrics.json \
  --out <run-dir>/result.docx \
  --charts <run-dir>

python .agents/skills/equity-workup/scripts/audit_report.py \
  --run <run-dir> \
  --template "Workup_template_260617-外测版.docx" \
  --render
```

Dependencies: `python-docx`, `matplotlib` (and `pymupdf` for `--render` page
images). Install with `pip install -e '.[dev]'` or `uv pip install python-docx
matplotlib pymupdf`.

## Outputs

At least: `run-plan.json`, `evidence.json`, `gaps.json`, `metrics.json`,
`working-template.docx`, `template-manifest.json`, `result.docx`, `audit.json`,
`standalone-stock-chart.png`, `competitor-comparison-chart.png`, and
`renders/page-N.png`.

## Acceptance

- Skill discoverable by Codex and OpenCode (this directory and `SKILL.md`).
- Exactly one target + two named competitors everywhere; no third competitor.
- Every written field has complete evidence or a deterministic calculation.
- Every gap has a reason and attempts.
- Manual regions untouched; original template hash unchanged.
- `result.docx` opens, renders, and passes the page-layout checks.
