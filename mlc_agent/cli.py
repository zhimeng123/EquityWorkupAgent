from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mlc_agent.exceptions import WorkupAgentError
from mlc_agent.service import run_workup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mlc-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Generate a fixed-template company workup")
    run_parser.add_argument("--template", type=Path, required=True)
    run_parser.add_argument("--company", required=True)
    run_parser.add_argument(
        "--goal",
        default="Generate an A-share listed company information workup report",
    )
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--yes", action="store_true", help="Skip plan confirmation")
    run_parser.add_argument("--debug", action="store_true")
    run_parser.add_argument("--keep-intermediate", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command != "run":
        raise SystemExit(2)
    try:
        result = run_workup(
            template=args.template,
            company=args.company,
            output=args.output,
            goal=args.goal,
            auto_confirm=args.yes,
            debug=args.debug,
            keep_intermediate=args.keep_intermediate,
        )
    except WorkupAgentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not result.get("self_check_result", {}).get("passed"):
        raise SystemExit(3)


if __name__ == "__main__":
    main()

