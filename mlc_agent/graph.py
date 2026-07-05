from __future__ import annotations

from functools import wraps
from time import perf_counter

from langgraph.graph import END, START, StateGraph

from mlc_agent.nodes import (
    confirm_plan_node,
    create_execution_plan_node,
    fetch_eastmoney_node,
    fetch_official_site_node,
    fetch_xueqiu_node,
    finalize_run_node,
    generate_evidence_files_node,
    initialize_run_node,
    load_template_mapping_node,
    merge_fields_node,
    resolve_company_node,
    self_check_node,
    write_docx_node,
)
from mlc_agent.schemas import WorkupAgentState
from mlc_agent.logging_utils import get_logger
from mlc_agent.integration import (
    collect_shared_disclosures_node,
    part01_node,
    part02_node,
    part03_node,
    part04_node,
    part05_node,
    part06_node,
    part07_node,
    part08_node,
    part09_node,
    part10_node,
    part11_node,
)


def _observable_part(name, node):
    @wraps(node)
    def run(state):
        started = perf_counter()
        get_logger().info("Node %s started", name)
        try:
            return node(state)
        finally:
            get_logger().info("Node %s finished in %.3fs", name, perf_counter() - started)

    return run


def build_workup_graph():
    builder = StateGraph(WorkupAgentState)
    nodes = [
        ("initialize_run", initialize_run_node),
        ("load_template_mapping", load_template_mapping_node),
        ("create_execution_plan", create_execution_plan_node),
        ("confirm_plan", confirm_plan_node),
        ("resolve_company", resolve_company_node),
        ("fetch_eastmoney", fetch_eastmoney_node),
        ("fetch_xueqiu", fetch_xueqiu_node),
        ("fetch_official_site", fetch_official_site_node),
        ("shared_disclosures", collect_shared_disclosures_node),
        ("part_01", part01_node),
        ("part_02", part02_node),
        ("part_03", part03_node),
        ("part_04", part04_node),
        ("part_05", part05_node),
        ("part_06", part06_node),
        ("part_07", part07_node),
        ("part_08", part08_node),
        ("part_09", part09_node),
        ("part_10", part10_node),
        ("part_11", part11_node),
        ("merge_fields", merge_fields_node),
        ("write_docx", write_docx_node),
        ("generate_evidence_files", generate_evidence_files_node),
        ("self_check", self_check_node),
        ("finalize_run", finalize_run_node),
    ]
    for name, node in nodes:
        observed = (
            _observable_part(name, node)
            if name == "shared_disclosures" or name.startswith("part_")
            else node
        )
        builder.add_node(name, observed)
    builder.add_edge(START, nodes[0][0])
    for (current, _), (following, _) in zip(nodes, nodes[1:]):
        builder.add_edge(current, following)
    builder.add_edge(nodes[-1][0], END)
    return builder.compile()
