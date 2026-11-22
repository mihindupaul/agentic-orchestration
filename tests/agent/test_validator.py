"""The validator: a spec against the deck. Plain code and a demo deck, no API, no Orca."""

import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anthropic")                     # importing the agent package loads the planner
from agent.inventory import ALL_WELLS, DemoInventory  # noqa: E402
from agent.spec import WorkflowSpec  # noqa: E402
from agent.validator import validate  # noqa: E402


def spec(*processes) -> WorkflowSpec:
    """spec((id, source bay, rows, destination bay), ...), every process scanning."""
    return WorkflowSpec.model_validate({"name": "t", "summary": "t", "processes": [
        {"id": pid, "source": {"bay": sb, "rows": rows}, "steps": [{"op": "scan"}],
         "destination": {"bay": db}} for pid, sb, rows, db in processes]})


def test_a_plan_that_fits_the_deck_lists_the_vials_each_process_moves():
    report = validate(spec(("a", 3, "CE", 8), ("b", 3, "DF", 8)), DemoInventory())
    assert report.ok and not report.warnings
    assert len(report.vials["a"]) == 24 and report.vials["a"][:2] == ["C1", "C2"] and "E12" in report.vials["a"]


def test_missing_racks_are_errors():
    report = validate(spec(("a", 5, "A", 8), ("b", 3, "A", 6)), DemoInventory())
    assert report.errors == ["a: bay 5 has no rack to take vials from", "b: bay 6 has no rack to put vials into"]


def test_an_empty_row_is_an_error_and_gaps_in_a_row_are_skipped_with_a_warning():
    deck = DemoInventory({3: [w for w in ALL_WELLS if w[0] != "B" and w not in ("C3", "C7")], 8: []})
    report = validate(spec(("a", 3, "BC", 8)), deck)
    assert report.errors == ["a: row B of bay 3 holds no vials"]
    assert report.warnings == ["a: 2 empty well(s) in row C of bay 3 will be skipped (C3, C7)"]
    assert "C3" not in report.vials["a"] and len(report.vials["a"]) == 10


def test_taken_destination_wells_are_an_error_unless_the_vial_goes_back_to_its_own_well():
    deck = DemoInventory({3: list(ALL_WELLS), 8: ["A1", "A2"]})
    assert validate(spec(("a", 3, "A", 8)), deck).errors == [
        "a: 2 destination well(s) in bay 8 already hold a vial (A1, A2)"]
    assert validate(spec(("a", 3, "A", 3)), deck).ok                 # back to the well it came from


def test_two_processes_may_not_share_vials_or_destination_wells():
    deck = DemoInventory({3: list(ALL_WELLS), 4: list(ALL_WELLS), 8: []})
    shared_source = validate(spec(("a", 3, "AB", 8), ("b", 3, "B", 8)), deck)
    assert any("b and a both take the vials in bay 3 B1, B2" in e for e in shared_source.errors)
    shared_destination = validate(spec(("a", 3, "A", 8), ("b", 4, "A", 8)), deck)
    assert shared_destination.errors == ["b and a both put vials into bay 8 A1, A2, A3, A4, A5, A6 and 6 more"]


def test_submit_workflow_returns_deck_errors_for_claude_to_fix():
    from agent.tools import PlanBox, make_tools
    box = PlanBox()
    submit = {t.name: t for t in make_tools(DemoInventory({3: list(ALL_WELLS), 8: ["C1"]}), box)}["submit_workflow"]
    plan = json.loads(spec(("a", 3, "C", 8)).model_dump_json())
    with pytest.raises(Exception, match="already hold a vial"):
        submit.call(plan)
    assert box.spec is None
