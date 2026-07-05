from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mlc_agent.graph import build_workup_graph
from mlc_agent.schemas import WorkupAgentState


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_workup(
    *,
    template: Path,
    company: str,
    output: Path,
    goal: str,
    auto_confirm: bool,
    debug: bool,
    keep_intermediate: bool,
) -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    initial_state: WorkupAgentState = {
        "template_path": str(template),
        "company_input": company,
        "output_root": str(output),
        "goal": goal,
        "auto_confirm": auto_confirm,
        "debug": debug,
        "keep_intermediate": keep_intermediate,
        "config_dir": str(PROJECT_ROOT / "configs"),
    }
    graph = build_workup_graph()
    return graph.invoke(initial_state)

