# Evidence Policy

The value of this report is that every filled cell can be traced to a source or a
deterministic calculation. This policy defines what counts as acceptable evidence
and how the deterministic tools enforce it.

## 1. Core rule

A field is written to `result.docx` only if it is `supported`, `derived`,
`not_disclosed`, or `not_applicable`. Everything else is `unresolved` and goes to
`gaps.json`. The tools never invent a value, never upgrade a weak finding, and
never weaken this policy to raise a fill rate.

## 2. Source quality ladder

Prefer sources in this order and record the highest available:

1. Company annual / interim / quarterly reports (audited financials).
2. Exchange and regulator filings (annual reports, announcements, penalty
   notices, inquiry letters).
3. Company official website and official investor-relations material.
4. Market-data providers for prices, market cap, and index values.
5. Reputable media for events, only when no primary source exists.

Search-engine result pages, snippets, and LLM output are **discovery aids only**.
The evidence must resolve to the primary URL and quote it.

## 3. What each status requires

### `supported`

The cited document directly states the value. Required: `source_url`,
`source_title`, `source_type`, `published_at`, `captured_at`, `page_or_section`,
`evidence_quote`. The quote must contain the value (or the inputs from which a
reader can read it) and be copied verbatim from the source.

### `derived`

The value comes from a deterministic formula over cited inputs. Required:
`calculation` (formula plus the substituted numbers) and `derived_from`
(the input field ids or source ids). The cited inputs must themselves be
`supported`. Examples: current ratio, quick ratio, CAPEX to revenue, interest
coverage, debt to asset, AR growth vs revenue growth, intangibles share, 24-month
return, maximum drawdown, peer medians, align-to-index/peers.

### `not_disclosed`

Used only after checking the relevant formal disclosure scope (for example, the
latest annual report's related-party section and the intervening exchange
announcements). Required: the same citation fields as `supported`, plus a quote
or page reference showing the scope that was checked. "I did not find it in a
search" is **not** `not_disclosed`.

### `not_applicable`

Used when evidence proves the item does not apply (for example, no US operations
and the report states so). Same citation requirements as `not_disclosed`.

### `unresolved`

Anything else: missing, conflicting, paywalled, unreachable, or unverifiable.
Record `reason` and `attempts` (each attempt names the path tried and the
outcome). `unresolved` is never written into the report and never rendered as
`No` / `Not disclosed`.

## 4. Retry discipline

1. Collect reusable primary documents first (annual/interim reports, filings).
2. Save each field's evidence as soon as the field is resolved — never
   reconstruct sources at the end.
3. After the first pass, review unresolved fields. Retry only when a specific
   alternative source or extraction method exists.
4. Each field gets at most two effective evidence paths; a full retry round with
   no new results ends the retry loop for that field.
5. Record every attempt so `gaps.json` explains the gap.

## 5. Business measurement conventions (MVP)

These are fixed; do not change them:

- SOE status follows the ultimate actual controller (state-owned or not).
- Listed outside directorship checks only current directors' seats at other
  listed companies.
- "Material": use the company / auditor / exchange / regulator's own
  "major / important / material asset restructuring" label. For impairments, also
  treat an amount reaching 10% of the latest complete-year net profit attributable
  to shareholders as material; when that net profit is not positive, rely only on
  a formal material label.
- Rolling windows use the run date as the cut-off.
- Peer Net Margin = net profit attributable to shareholders / revenue. Inventory
  Turnover uses the provider's disclosed same-period metric; do not re-derive it.
- Liquidity Previous/Current = the latest complete year and the year before it.
- Current Ratio = current assets / current liabilities.
- Quick Ratio = (current assets − inventory) / current liabilities.
- CAPEX = cash paid to acquire fixed assets, intangibles, and other long-term
  assets, reported as a positive number.
- CAPEX to Revenue = CAPEX / revenue.
- Interest Coverage = (operating profit + interest expense) / interest expense.
- Debt to Asset = total liabilities / total assets.
- NOI = net profit attributable to shareholders. `Cash Flow > NOI` compares net
  operating cash flow with NOI.
- Significant short-term debt pressure = latest period cash < short-term
  borrowings. Positive operating cash flow requires both latest complete years
  to be positive.
- AR growth uses only the balance-sheet "accounts receivable" line; the
  write-down ratio denominator is the period-end gross accounts receivable.
- Prices: performance, drawdown, and charts use forward-adjusted daily closes;
  current price and 52-week high/low use unadjusted quotes. Shenzhen / Shanghai /
  Beijing use SZSE Component / SSE Composite / BSE 50. Align means within 15
  percentage points of the benchmark or peer median over 24 months; a significant
  drop is a maximum peak-to-trough drawdown of at least 30% over 24 months.
- Charts: company chart uses forward-adjusted price; peer chart is normalised to
  100 on day one. Width 6.3 in, PNG at least 1600×750.
- Major shareholders: same-period top ten. Insider relationships only when the
  report states them; otherwise `Not disclosed` — never inferred.
- Litigation: include still-pending material cases regardless of age; historical
  cases roll back 24 months.
- Negative news: 12 months, Chinese and English. The engine may be used for
  discovery only; evidence must land on the original media / company / exchange /
  regulator URL. Attachments are DOCX. When the body cannot be fetched, keep only
  date, title, source, URL, and `Full text unavailable` — never summarise a body
  that was not retrieved.
- Names: prefer official English names; when none exists keep the Chinese proper
  noun (no invented translation or pinyin). Surrounding prose stays English.

## 6. Integrity checks the tools perform

`audit_report.py` fails the run when:

- a written field lacks the evidence required for its status;
- an `unresolved` field appears in `result.docx`;
- a manual-only region differs from the original template;
- the original template hash changed;
- the document contains anything other than the target and the two named
  competitors in peer tables/charts;
- a required artifact is missing or the document cannot be opened.
