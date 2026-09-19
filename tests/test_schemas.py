import pytest
from pydantic import ValidationError

from mlc_agent.schemas import ExecutionStep


@pytest.mark.parametrize("status", ["pending", "running", "completed", "partial", "failed"])
def test_execution_step_accepts_all_contract_statuses(status):
    step = ExecutionStep(step_id="part_05", title="Peer analysis", status=status)

    assert step.status == status


def test_execution_step_rejects_status_outside_contract():
    with pytest.raises(ValidationError):
        ExecutionStep(step_id="part_05", title="Peer analysis", status="skipped")
