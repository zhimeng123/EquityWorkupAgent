from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from mlc_agent.annual_report_validation import build_local_annual_bundle
from mlc_agent.production_adapters import extract_part05_related_party_transactions
from mlc_agent.related_parties import collect_related_party_transactions
from mlc_agent.report_parser import parse_machine_generated_pdf


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate related-party transactions from one local annual-report PDF"
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--stock-code", required=True)
    parser.add_argument("--report-year", required=True, type=int)
    parser.add_argument("--published-at", required=True, type=datetime.fromisoformat)
    args = parser.parse_args()

    parsed = parse_machine_generated_pdf(args.pdf.resolve())
    bundle = build_local_annual_bundle(
        parsed,
        stock_code=args.stock_code,
        report_year=args.report_year,
        published_at=args.published_at,
    )
    transactions = extract_part05_related_party_transactions(
        None, model="local-deterministic", bundle=bundle
    )
    value = collect_related_party_transactions(
        transactions, captured_at=datetime.now(timezone.utc)
    )
    result = {
        "input_pdf": str(args.pdf.resolve()),
        "network_scope": "offline; no model or source websites accessed",
        "transaction_count": len(transactions),
        "source_value": value.model_dump(mode="json") if value else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(args.output)


if __name__ == "__main__":
    main()
