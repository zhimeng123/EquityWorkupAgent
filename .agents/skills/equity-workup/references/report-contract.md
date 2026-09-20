# Report Contract

This contract is the single source of truth for what the Equity Workup Skill may
fill into the Word template, where it writes, and what every written value must
carry. The deterministic tooling (`scripts/write_report.py`, `scripts/audit_report.py`)
enforces this contract; the research playbook explains how to satisfy it.

## 1. Run inputs (non-negotiable)

A run accepts exactly:

- one target A-share listed company;
- two competitor A-share listed companies chosen by the human user;
- an optional research cut-off date (defaults to the run date).

The Skill may only **resolve and confirm** these three identities. By default it
must never discover, recommend, rank, replace, or add competitors. If any of the
three is missing, duplicated, or not an A-share listed company, stop and ask the
user.

**Guarded exception.** The user may explicitly authorize the agent to select the
two competitors. Then `run-plan.json` must record:

```json
"peer_selection": {
  "mode": "user_authorized_auto",
  "authorization": "verbatim user authorization",
  "rationale": "why these two A-share listed companies are comparable"
}
```

The default is `{"mode": "user_named"}`. `audit_report.py` requires a valid mode,
requires `authorization` and `rationale` for auto mode, and requires each peer to
carry a 6-digit A-share ticker and an exchange in `SSE` / `SZSE` / `BSE`.

`run-plan.json` records the three confirmed identities and the cut-off date. Every
downstream artifact is scoped to exactly these three companies.

## 2. Field status vocabulary

A field value is written only when its status is one of:

| status | meaning | written to report |
| --- | --- | --- |
| `supported` | a cited source directly states the value | yes |
| `derived` | a deterministic calculation over cited inputs | yes |
| `not_disclosed` | the relevant formal disclosure scope was checked and does not disclose it | yes, as `Not disclosed` |
| `not_applicable` | evidence proves the item does not apply | yes, as `Not applicable` / `N/A` |
| `unresolved` | evidence is missing, conflicting, unreachable, or unverifiable | **no** — gaps list only |

`unresolved` must never be rendered as `No`, `None`, `Not disclosed`, or any other
definite conclusion. An empty search result is `unresolved`, not `No`. LLM output
is never a fact source; a synthesis may only be `derived` from already-supported
field ids (see §5).

## 3. Field registry

Field ids are stable. `write_report.py` binds each id to one bookmark in the
working template (`EQW_<id with . replaced by _>`); `audit_report.py` verifies the
bindings. `keep_prefix` is the template label kept in front of the value.

### 3.1 Company overview

| field_id | template slot | keep_prefix |
| --- | --- | --- |
| `co.country_of_incorp` | Company Overview table, `Country of Incorp.:` value cell | — |
| `co.soe_status` | `SOE / Non-SOE:` value cell | — |
| `co.total_asset` | `Total Asset:` value cell | — |
| `co.total_equity` | `Total Equity:` value cell | — |
| `co.annual_revenue` | `Annual Revenue:` value cell | — |
| `co.annual_net_profit` | `Annual Net Profit:` value cell | — |
| `co.listed_or_private` | `Is the Proposer Listed or Privately Owned?` value cell | — |
| `co.listed_exchange` | `Listed Exchange:` value cell | — |
| `co.market_cap` | `Market Capitalization:` value cell | — |
| `co.listed_subsidiary` | `Does the Proposer has any listed Subsidiary:` value cell | — |
| `co.listed_outside_directorship` | `Does the Proposer has any listed Outside Directorship` value cell | — |
| `co.ma_past_12m` | `Any major M&A during the past 12 months?` cell | question text |
| `co.ma_plan_next_12m` | standalone paragraph `Any M&A plan in the next 12 months?` | question text |
| `co.website` | paragraph `Official website link:` | `Official website link: ` |
| `co.google_finance` | paragraph `Google finance link:` | `Google finance link: ` |
| `co.cninfo` | paragraph `巨潮资讯网链接：` | `巨潮资讯网链接：` |
| `co.xueqiu` | paragraph `雪球网链接：` | `雪球网链接：` |
| `co.eastmoney` | paragraph `东方财富网链接：` | `东方财富网链接：` |
| `co.business_description` | Business DESCRIPTION instruction paragraph | — |

### 3.2 US exposure

| field_id | template slot | keep_prefix |
| --- | --- | --- |
| `us.subsidiaries` | `US Subsidiaries:` value cell | — |
| `us.num_subsidiaries` | `# of US Subsidiary:` value cell | — |
| `us.other_details` | `Other Details:` value cell | — |
| `us.revenue` | `US Revenue:` value cell | — |
| `us.revenue_pct` | `% of Total Revenue:` value cell | — |
| `us.details_of_sub` | `Details of US Sub.:` value cell | — |
| `us.employees` | `US Employees:` value cell | — |
| `us.split_by_state` | `Split by State:` value cell | — |

### 3.3 Operating performance

| field_id | template slot | keep_prefix |
| --- | --- | --- |
| `op.revenue_breakdown` | `Revenue Breakdown By Business Segment & Geography:` cell | label |
| `op.revenue_trend` | `Revenue Trend & Profitability` instruction cell | — |
| `op.outlook_risks` | `Business outlook ...` instruction cell | — |
| `op.related_party` | `Any related party transaction ...` instruction cell | — |
| `op.peer_confirmation` | `Confirmation ... in line with Peers:` value cell | — |
| `op.peer.proposer.name` | nested peer table, Proposer name cell | — |
| `op.peer.proposer.revenue` | nested peer table, Proposer `Revenue` cell | — |
| `op.peer.proposer.inventory_turnover` | nested peer table, Proposer `Inventory Turnover` cell | — |
| `op.peer.proposer.gross_margin` | nested peer table, Proposer `Gross Margin` cell | — |
| `op.peer.proposer.net_margin` | nested peer table, Proposer `Net Margin` cell | — |
| `op.peer1.name` | nested peer table, competitor 1 name cell | — |
| `op.peer1.revenue` | nested peer table, competitor 1 `Revenue` cell | — |
| `op.peer1.inventory_turnover` | nested peer table, competitor 1 `Inventory Turnover` cell | — |
| `op.peer1.gross_margin` | nested peer table, competitor 1 `Gross Margin` cell | — |
| `op.peer1.net_margin` | nested peer table, competitor 1 `Net Margin` cell | — |
| `op.peer2.name` | nested peer table, competitor 2 name cell | — |
| `op.peer2.revenue` | nested peer table, competitor 2 `Revenue` cell | — |
| `op.peer2.inventory_turnover` | nested peer table, competitor 2 `Inventory Turnover` cell | — |
| `op.peer2.gross_margin` | nested peer table, competitor 2 `Gross Margin` cell | — |
| `op.peer2.net_margin` | nested peer table, competitor 2 `Net Margin` cell | — |

The nested peer table contains **exactly** the Proposer and two competitors after
preparation. No third competitor row may remain anywhere in the document.

### 3.4 Liquidity & debt

Each ratio has a `_prev` (latest complete year minus one) and `_curr` (latest
complete year) slot.

| field_id | template slot |
| --- | --- |
| `liq.current_ratio_prev` / `liq.current_ratio_curr` | `Current Ratio:` Previous / Current |
| `liq.quick_ratio_prev` / `liq.quick_ratio_curr` | `Quick Ratio:` Previous / Current |
| `liq.capex_prev` / `liq.capex_curr` | `CAPEX:` Previous / Current |
| `liq.capex_to_revenue_prev` / `liq.capex_to_revenue_curr` | `CAPEX to Revenue:` Previous / Current |
| `liq.interest_coverage_prev` / `liq.interest_coverage_curr` | `Interest Coverage Ratio:` Previous / Current |
| `liq.debt_to_asset_prev` / `liq.debt_to_asset_curr` | `Debt to Asset Ratio:` Previous / Current |
| `liq.ocf_positive_prev` / `liq.ocf_positive_curr` | `OCF Positive (Y/N):` Previous / Current |
| `liq.cashflow_gt_noi_prev` / `liq.cashflow_gt_noi_curr` | `Cash Flow > NOI (Y/N):` Previous / Current |
| `liq.short_term_debt_concern` | `Are there significant amount of short term debt ...` value cell |
| `liq.positive_ocf` | `Is the Company generating positive operation cash flow?` value cell |
| `liq.net_income_exceed_ocf` | `Does net income exceed cash flow from operating activities?` value cell |
| `liq.comments` | `COMMENTS BY AI if no extra charge` cell |

### 3.5 Financial analysis

| field_id | template slot | keep_prefix |
| --- | --- | --- |
| `fin.ar_growth_vs_revenue` | `Is account receivable growth higher than revenue growth?` value cell | — |
| `fin.one_time_writedown` | `Has the company taken a significant one-time write-down ...` value cell | — |
| `fin.intangibles_gt_25pct` | `Do intangibles represent more than 25% ...` value cell | — |

### 3.6 Security exposure

| field_id | template slot |
| --- | --- |
| `sec.ipo_date` | `IPO Date:` value cell |
| `sec.total_market_cap` | `Total Market Cap:` value cell |
| `sec.current_price` | `Current Price:` value cell |
| `sec.52w_low` | `52 Week Low:` value cell |
| `sec.52w_high` | `52 Week High:` value cell |
| `sec.align_index` | `Align to Index:` value cell |
| `sec.align_peers` | `Align to Peers:` value cell |
| `sec.significant_drop` | `Was there a specific & significant stock drop ...` value cell |
| `sec.drop_details` | first `If yes to the above, please provide full detail:` in the drop table |
| `sec.securities_offerings` | `Has the Company made any securities offerings ...` value cell |
| `sec.offerings_details` | second `If yes to the above, please provide full detail:` in the drop table |
| `sec.charts` | paragraph `可在雪球网上抓取股价走势图` (image slot) |

### 3.7 Corporate governance

| field_id | template slot | keep_prefix |
| --- | --- | --- |
| `cg.management_highlights` | `Highlight of Chairman, CEO, CFO ...` instruction cell | — |
| `cg.board_structure` | `Structure of the Board ...` cell | label |
| `cg.director_changes` | `Whether there is any change to directors or officers ...` cell | label |

### 3.8 Shareholders & employees

| field_id | template slot | keep_prefix |
| --- | --- | --- |
| `sh.major_shareholders` | nested instruction cell under `List of Major Shareholders ...` | — |
| `sh.major_change_12m` | `Has there been any major change in substantial shareholders ...` value cell | — |
| `sh.change_details` | `If yes to the above, please provide full detail:` in shareholder table | — |
| `emp.headcount` | `# of Employees:` row, PRC / USA / Europe / ROW / Total cells | — |

`emp.headcount` is a multi-slot field: its value is an object with keys
`prc`, `usa`, `europe`, `row`, `total`; each key maps to its own cell.

### 3.9 Audit & non-financial changes

| field_id | template slot | keep_prefix |
| --- | --- | --- |
| `au.auditor` | `Auditor; If non-BIG 4 ...` value cell | — |
| `au.opinion` | `Opinion:` value cell | — |
| `au.qualified_details` | `If "Qualified", please provide full detail:` value cell | — |
| `au.auditor_change` | `Has the Proposer changed its auditors in the past 2 years:` value cell | — |
| `au.auditor_change_details` | `If yes to the above, ...` after auditor change | — |
| `au.restatement` | `Has the Proposer restated their financial ...` value cell | — |
| `au.restatement_details` | `If yes to the above, ...` after restatement | — |
| `nf.board_change` | `Major changes in board structure ...` value cell | — |
| `nf.business_change` | `Material change in business operations?` value cell | — |
| `nf.top3_shareholder_change` | `Changes to top 3 shareholders?` value cell | — |
| `nf.details` | `If yes to any of the above, please provide full details:` value cell | — |

### 3.10 Litigation & news

| field_id | template slot |
| --- | --- |
| `lit.pending` | `Are there any pending & prior litigations ...` value cell |
| `lit.regulatory` | `Has there been any regulatory ... investigation or penalty ...` value cell |
| `lit.details` | `If yes to the above, please provide full detail:` in litigation table |
| `news.negative_news` | `News SEARCH` section cell |

## 4. Manual-only regions (never auto-filled)

`write_report.py` refuses any field bound to these regions, and `audit_report.py`
proves they still match the original template:

- General Account table: `Underwriter`, `Branch`, `Producer`, `Commission`,
  `Reason for Referral`, `Date Approval Req.`, `Overall Relationship with the
  company`, `Brief of Competition`, `Clearance Obtained`, `If yes, who from`,
  `NB or Renewal`, `Written since`, `Premium Earned`, `Claim History`.
- `RECOMMNEDATIONS`: `Rationale for Recommendation`, `Recommended – D&O`,
  `Recommended - POSI`, `Subjectivities`.
- `Technical Rating`: `Rated Premium`, `Rating Rationale between D&O + POSI`,
  `If indicated premium is below rated premium, please explain`.
- `Sign-off` and `Date`.

These keep their original blank/placeholder state. The Skill must not generate
underwriting opinion, pricing, referral, relationship, or sign-off content.

## 5. Evidence record schema

Every written field carries a record:

```json
{
  "field_id": "co.total_asset",
  "status": "supported",
  "value": "RMB 71,234,567,890",
  "period": "2024A",
  "source_url": "https://...",
  "source_title": "2024 Annual Report",
  "source_type": "annual_report",
  "published_at": "2025-03-28",
  "captured_at": "2026-09-20T10:00:00+08:00",
  "page_or_section": "Section 3 / p.45",
  "evidence_quote": "总资产 71,234,567,890 元",
  "calculation": null,
  "derived_from": []
}
```

Rules:

- `supported`: `source_url`, `source_title`, `source_type`, `published_at`,
  `captured_at`, `page_or_section`, `evidence_quote` are all required.
- `derived`: `calculation` is required and must name the formula and the input
  values; `derived_from` lists the field ids or source ids used. `source_url`
  must point to at least one cited input.
- `not_disclosed` / `not_applicable`: `source_url`, `source_title`,
  `source_type`, `published_at`, `captured_at`, `page_or_section`, and
  `evidence_quote` are required (they prove the scope that was checked).
- `unresolved`: no `value`; requires `reason` and an `attempts` list. Not written.

`source_type` is one of: `annual_report`, `interim_report`, `quarterly_report`,
`exchange_filing`, `regulator_filing`, `company_official`, `market_data`,
`media`, `other_primary`.

## 6. Run artifacts

A run directory contains at least:

| file | produced by | purpose |
| --- | --- | --- |
| `run-plan.json` | Agent | confirmed identities, cut-off date, field plan, retry plan |
| `evidence.json` | Agent | field records per §5 |
| `gaps.json` | Agent | unresolved fields with reasons and attempts |
| `metrics.json` | `calculate_metrics.py` | deterministic calculations |
| `standalone-stock-chart.png` | `calculate_metrics.py` | target 2-year adjusted-price chart |
| `competitor-comparison-chart.png` | `calculate_metrics.py` | target + 2 peers, normalised to 100 |
| `working-template.docx` | `write_report.py prepare` | template copy with peers 3→2 and bookmarks |
| `template-manifest.json` | `write_report.py prepare` | original hash, working hash, bookmark map |
| `result.docx` | `write_report.py write` | final report |
| `audit.json` | `audit_report.py` | acceptance result |
| `renders/` | `audit_report.py` | per-page PNGs for human layout review |

Chart requirements: PNG at least 1600×750 px; inserted at 6.3 in wide, height
proportional. The comparison chart is normalised to 100 on the first day and
includes only the target and the two named competitors.

## 7. Formatting conventions

- Money: original reporting currency with thousands separators and the unit
  stated, e.g. `RMB 1,234,567,890` or `RMB 12.35 bn`.
- Ratios: two decimals (e.g. `1.42`). Percentages: two decimals with `%`.
- Yes/No fields: exactly `Yes` or `No`; use `Not disclosed` / `Not applicable`
  for those statuses.
- `Align to Index` / `Align to Peers`: `Align` or `Not align`, with the numeric
  difference in the comment slot.
- Multi-line values use `\n`; `write_report.py` renders them as line breaks.
