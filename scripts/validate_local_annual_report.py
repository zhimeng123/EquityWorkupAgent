from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

from mlc_agent.annual_report_validation import run_annual_report_validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate annual-report-backed fields from one local PDF")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--stock-code", required=True)
    parser.add_argument("--report-year", required=True, type=int)
    parser.add_argument("--published-at", required=True, type=datetime.fromisoformat)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    output = run_annual_report_validation(
        args.pdf,
        args.output_dir,
        stock_code=args.stock_code,
        report_year=args.report_year,
        published_at=args.published_at,
        as_of=args.as_of,
    )
    print(output)


if __name__ == "__main__":
    main()
