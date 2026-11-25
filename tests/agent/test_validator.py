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
        "a: 2 destination well(s) in bay 8 hold a vial the plan does not move away (A1, A2)"]
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
    with pytest.raises(Exception, match="hold a vial the plan does not move away"):
        submit.call(plan)
    assert box.spec is None


def test_column_and_well_sources_are_checked_against_the_deck():
    deck = DemoInventory({3: [w for w in ALL_WELLS if w not in ("E1", "F1", "G1", "H1")], 8: []})
    plan = WorkflowSpec.model_validate({"name": "t", "summary": "t", "processes": [
        {"id": "col", "source": {"bay": 3, "columns": [1]}, "steps": [], "destination": {"bay": 8}},
        {"id": "pick", "source": {"bay": 3, "wells": ["A6", "B6", "C6", "D6"]}, "steps": [], "destination": {"bay": 8}}]})
    report = validate(plan, deck)
    assert report.ok and report.vials == {"col": ["A1", "B1", "C1", "D1"], "pick": ["A6", "B6", "C6", "D6"]}
    assert report.warnings == ["col: 4 empty well(s) in column 1 of bay 3 will be skipped (E1, F1, G1, H1)"]


# ------------------------------------------------------------------------- destination wells and transit wells
def plan(*processes, transit=()) -> WorkflowSpec:
    """plan((id, source, destination), ..., transit=[(bay, well)]): plain moves."""
    return WorkflowSpec.model_validate({"name": "t", "summary": "t",
                                        "transit": [{"bay": b, "well": w} for b, w in transit],
                                        "processes": [{"id": pid, "source": src, "steps": [], "destination": dst}
                                                      for pid, src, dst in processes]})


def test_destination_rows_columns_and_wells_pair_with_the_source():
    rows, cols, wells = plan(("r", {"bay": 3, "rows": "AB"}, {"bay": 3, "rows": "GH"}),
                             ("c", {"bay": 3, "columns": [1]}, {"bay": 8, "columns": [12]}),
                             ("w", {"bay": 4, "wells": ["A1", "B1"]}, {"bay": 4, "wells": ["H12", "H11"]})).processes
    assert rows.pairs()[:2] == [("A1", "G1"), ("A2", "G2")] and rows.pairs()[12] == ("B1", "H1")
    assert cols.pairs()[0] == ("A1", "A12") and cols.pairs()[7] == ("H1", "H12")
    assert wells.pairs() == [("A1", "H12"), ("B1", "H11")]
    assert rows.destination.describe() == "bay 3 rows GH"


@pytest.mark.parametrize("src, dst, message", [
    ({"rows": "AB"}, {"rows": "H"}, "one row per source row"),
    ({"rows": "A"}, {"columns": [1]}, "need a source given as columns"),
    ({"wells": ["A1", "A2"]}, {"wells": ["B1"]}, "one well per source well"),
    ({"wells": ["A1", "A2"]}, {"wells": ["B1", "B1"]}, "same destination well"),
])
def test_a_destination_that_does_not_fit_its_source_is_rejected(src, dst, message):
    with pytest.raises(ValueError, match=message):
        plan(("p", {"bay": 3, **src}, {"bay": 3, **dst}))


def test_a_destination_well_may_hold_a_vial_the_plan_moves_away():
    deck = DemoInventory({3: ["A1", "B1"]})
    chain = plan(("p", {"bay": 3, "wells": ["A1", "B1"]}, {"bay": 3, "wells": ["B1", "C1"]}))
    report = validate(chain, deck)
    assert report.ok and not report.routes and report.moves == {"p": [("A1", "B1"), ("B1", "C1")]}


def test_a_swap_needs_one_free_transit_well_per_cycle():
    deck = DemoInventory({3: [f"{r}{c}" for r in "AH" for c in range(1, 13)]})
    swap = (("a_to_h", {"bay": 3, "rows": "A"}, {"bay": 3, "rows": "H"}),
            ("h_to_a", {"bay": 3, "rows": "H"}, {"bay": 3, "rows": "A"}))
    report = validate(plan(*swap), deck)
    assert len(report.errors) == 1
    assert report.errors[0].startswith("12 set(s) of moves form a cycle (bay 3 A1 -> bay 3 H1 -> back;")
    assert "add 12 more to transit" in report.errors[0]

    transit = [(3, f"E{c}") for c in range(1, 13)]
    report = validate(plan(*swap, transit=transit), deck)
    assert report.ok and len(report.routes) == 12
    assert report.routes[(3, "A1")] == (3, "E1") and (3, "H1") not in report.routes   # one vial per cycle waits


def test_a_transit_well_must_be_free_and_unused_by_the_plan():
    deck = DemoInventory({3: ["A1", "H1", "D4"], 5: []})
    swap = (("x", {"bay": 3, "wells": ["A1"]}, {"bay": 3, "wells": ["H1"]}),
            ("y", {"bay": 3, "wells": ["H1"]}, {"bay": 3, "wells": ["A1"]}))
    assert "transit well bay 3 D4 holds a vial" in validate(plan(*swap, transit=[(3, "D4")]), deck).text()
    assert "is a source or destination" in validate(plan(*swap, transit=[(3, "A1")]), deck).text()
    assert "bay 6 has no rack" in validate(plan(*swap, transit=[(6, "A1")]), deck).text()
    assert validate(plan(*swap, transit=[(3, "E1")]), deck).ok


# ------------------------------------------------------------------------- routes: destinations decided per vial
def routed(dst_bay=8, default=None, steps=({"op": "weigh"},), wells=("C1", "C2")) -> WorkflowSpec:
    """Weigh the wells of bay 3; grams > 2 -> bay `dst_bay`, otherwise `default` (back to bay 3)."""
    return WorkflowSpec.model_validate({"name": "t", "summary": "t", "processes": [
        {"id": "p", "source": {"bay": 3, "wells": list(wells)}, "steps": list(steps),
         "routes": [{"when": {"value": "grams", "gt": 2}, "then": {"bay": dst_bay}}],
         "destination": default or {"bay": 3}}]})


def test_a_route_decides_where_a_vial_goes_and_falls_back_to_the_destination():
    from agent.spec import decide_route
    process = routed().processes[0]
    assert decide_route(process, {"grams": 2.3})[0].bay == 8
    assert decide_route(process, {"grams": 1.9}) == (process.destination, None)
    assert decide_route(process, {}) == (process.destination, None)          # never measured: no route holds


def test_a_route_must_test_something_the_process_measures_and_fit_the_source():
    with pytest.raises(ValueError, match="no weigh step"):
        routed(steps=[{"op": "scan"}])
    with pytest.raises(ValueError, match="route 0: destination rows need a source given as rows"):
        WorkflowSpec.model_validate({"name": "t", "summary": "t", "processes": [
            {"id": "p", "source": {"bay": 3, "wells": ["C1"]}, "steps": [{"op": "weigh"}],
             "routes": [{"when": {"value": "grams", "gt": 2}, "then": {"bay": 8, "rows": "H"}}],
             "destination": {"bay": 3}}]})


def test_back_to_its_own_well_is_fine_and_every_route_well_must_be_free():
    deck = DemoInventory({3: ["C1", "C2"], 8: []})
    report = validate(routed(), deck)
    assert report.ok and report.candidates[(3, "C1")] == [(8, "C1"), (3, "C1")]
    taken = validate(routed(), DemoInventory({3: ["C1", "C2"], 8: ["C2"]}))
    assert taken.errors == ["p: 1 well(s) a vial may be sent to hold a vial (bay 8 C2); a vial with routes needs "
                            "free wells"]


def test_a_route_well_may_not_be_used_by_another_vial_of_the_plan():
    deck = DemoInventory({3: ["C1", "C2", "D1"], 8: []})
    plan = routed()
    other = {"id": "q", "source": {"bay": 3, "wells": ["D1"]}, "steps": [], "destination": {"bay": 8, "wells": ["C1"]}}
    clash = WorkflowSpec.model_validate({**plan.model_dump(), "processes": [plan.processes[0].model_dump(), other]})
    assert "p: 1 well(s) a vial may be sent to are used by other vials of the plan (bay 8 C1)" in validate(clash, deck).errors
