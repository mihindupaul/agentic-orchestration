"""The planning agent without the API: the spec, the tools, and the planner's wiring.

`ScriptedClient` stands in for `anthropic.Anthropic`. Its tool runner plays a fixed script of tool calls
against the *real* tool functions (so their results and errors are real), then a final reply. That checks
everything on our side of the API: what the planner sends, how the tools answer, what comes back out."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("pydantic")
from pydantic import ValidationError  # noqa: E402

from agent import DemoInventory, PlanningError, WorkflowSpec, plan_workflow  # noqa: E402
from agent.planner import MODEL, SYSTEM_PROMPT  # noqa: E402
from agent.tools import PlanBox, make_tools  # noqa: E402

GOOD_SPEC = {
    "version": 1, "name": "rows_c_to_f", "summary": "C and E scanned and weighed; D and F heated to 40 C.",
    "processes": [
        {"id": "scan_weigh", "source": {"bay": 3, "rows": "CE"},
         "steps": [{"op": "scan", "tries": 3}, {"op": "weigh"}], "destination": {"bay": 8, "wells": "same"}},
        {"id": "heat", "source": {"bay": 3, "rows": "DF"},
         "steps": [{"op": "heat", "celsius": 40, "hold_s": 20}], "destination": {"bay": 8, "wells": "same"}},
    ],
}


# ------------------------------------------------------------------------------------------- the stand-in
def block(**kw):
    return SimpleNamespace(**kw)


class ScriptedClient:
    """`client.beta.messages.tool_runner(...)` that replays `turns` (lists of (tool, input)), then `reply`."""

    def __init__(self, turns, reply="Planned.", final_stop="end_turn"):
        self.turns, self.reply, self.final_stop = turns, reply, final_stop
        self.sent, self.results = None, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=self._tool_runner))

    def _tool_runner(self, **kwargs):
        self.sent = kwargs
        tools = {t.name: t for t in kwargs["tools"]}
        for turn in self.turns:
            content = [block(type="tool_use", name=name, input=args) for name, args in turn]
            yield block(content=content, stop_reason="tool_use", stop_details=None)
            for name, args in turn:                    # what the real runner does between round trips
                try:
                    self.results.append((name, tools[name].call(args), False))
                except Exception as exc:               # the real runner sends these back with is_error
                    self.results.append((name, str(exc), True))
        yield block(content=[block(type="text", text=self.reply)], stop_reason=self.final_stop,
                    stop_details=block(explanation="declined") if self.final_stop == "refusal" else None)


# ------------------------------------------------------------------------------------------- the spec
def test_the_example_spec_is_valid_and_lists_its_wells():
    spec = WorkflowSpec.model_validate(GOOD_SPEC)
    assert spec.processes[0].source.positions()[:2] == ["C1", "C2"] and len(spec.processes[0].source.positions()) == 24


@pytest.mark.parametrize("change", [
    lambda s: s["processes"][1]["steps"][0].update(celsius=150),            # above the heater's limit
    lambda s: s["processes"][0]["steps"].append({"op": "shake"}),           # no such step
    lambda s: s["processes"][0]["source"].update(rows="CZ"),                # no row Z
    lambda s: s["processes"][0]["destination"].update(wells="mirror"),      # only "same" exists
    lambda s: s.update(colour="blue"),                                      # unknown field: an error, not ignored
])
def test_the_spec_refuses_what_the_cell_cannot_do(change):
    bad = json.loads(json.dumps(GOOD_SPEC))
    change(bad)
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(bad)


# ------------------------------------------------------------------------------------------- the tools
def test_tools_answer_from_the_inventory():
    box = PlanBox()
    tools = {t.name: t for t in make_tools(DemoInventory(), box)}
    assert set(tools) == {"describe_cell", "rack_inventory", "submit_workflow", "mark_plan_ready"}
    assert "heater" in json.loads(tools["describe_cell"].call({}))["instruments"]
    bay3 = json.loads(tools["rack_inventory"].call({"bay": 3}))
    assert bay3["count"] == 96 and bay3["vials_per_row"]["C"] == 12
    assert json.loads(tools["rack_inventory"].call({"bay": 8}))["count"] == 0
    assert json.loads(tools["rack_inventory"].call({"bay": 5}))["present"] is False


def test_submit_workflow_shows_claude_the_spec_schema():
    submit = {t.name: t for t in make_tools(DemoInventory(), PlanBox())}["submit_workflow"]
    schema = submit.to_dict()["input_schema"]
    assert {"name", "summary", "processes"} <= set(schema["properties"])
    assert "Heat" in schema["$defs"] and schema["$defs"]["Heat"]["properties"]["celsius"]["maximum"] == 100


def test_a_rejected_spec_names_the_field_to_fix():
    box = PlanBox()
    submit = {t.name: t for t in make_tools(DemoInventory(), box)}["submit_workflow"]
    bad = json.loads(json.dumps(GOOD_SPEC))
    bad["processes"][1]["steps"][0]["celsius"] = 150
    with pytest.raises(Exception, match=r"processes\.1\.steps\.0\.heat\.celsius"):
        submit.call(bad)
    assert box.spec is None and len(box.attempts) == 1
    assert submit.call(GOOD_SPEC).startswith("accepted (scan_weigh: 24 vials, heat: 24 vials)")
    assert box.spec.name == "rows_c_to_f" and box.report.ok


# ------------------------------------------------------------------------------------------- the planner
def test_the_planner_sends_the_prompt_tools_and_settings_and_returns_the_spec():
    bad = json.loads(json.dumps(GOOD_SPEC))
    bad["processes"][1]["steps"][0]["celsius"] = 150
    client = ScriptedClient([
        [("describe_cell", {})],
        [("rack_inventory", {"bay": 3}), ("rack_inventory", {"bay": 8})],
        [("submit_workflow", bad)],                  # rejected: Claude gets the field error ...
        [("submit_workflow", GOOD_SPEC)],            # ... and submits a corrected spec
        [("mark_plan_ready", {"note": "C/E scanned and weighed, D/F heated, all into bay 8."})],
    ], reply="Rows C and E will be scanned and weighed, D and F heated.")
    result = plan_workflow("scan and weigh C and E, heat D and F, all to bay 8", DemoInventory(), client=client)

    sent = client.sent
    assert sent["model"] == MODEL and sent["system"] == SYSTEM_PROMPT
    assert sent["messages"] == [{"role": "user", "content": "scan and weigh C and E, heat D and F, all to bay 8"}]
    assert sent["thinking"] == {"type": "adaptive"} and sent["output_config"] == {"effort": "high"}
    assert sent["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in sent["betas"]
    assert "tool_choice" not in sent                 # forced tool choice is a 400 on this model

    assert [r[2] for r in client.results] == [False, False, False, True, False, False]   # only the bad spec erred
    assert result.spec == WorkflowSpec.model_validate(GOOD_SPEC) and result.ready
    assert result.submissions == 2 and result.reply.startswith("Rows C and E")
    assert [name for name, _ in result.tool_calls] == ["describe_cell", "rack_inventory", "rack_inventory",
                                                       "submit_workflow", "submit_workflow", "mark_plan_ready"]


def test_no_spec_when_claude_explains_instead_of_submitting():
    client = ScriptedClient([[("rack_inventory", {"bay": 3})]], reply="The cell has no shaker; nearest plan: ...")
    result = plan_workflow("shake row A", DemoInventory(), client=client)
    assert result.spec is None and "no shaker" in result.reply


@pytest.mark.parametrize("stop, message", [("refusal", "declined"), ("max_tokens", "cut off"),
                                           ("tool_use", "still calling tools")])
def test_a_reply_that_did_not_end_normally_is_an_error(stop, message):
    with pytest.raises(PlanningError, match=message):
        plan_workflow("anything", DemoInventory(), client=ScriptedClient([], final_stop=stop))


def test_env_file_fills_the_environment_but_never_overrides_it(tmp_path, monkeypatch):
    from agent.env import load_env
    (tmp_path / ".env").write_text("# comment\nAGENT_TEST_A=from-file\nAGENT_TEST_B='quoted'\n")
    (tmp_path / "sub").mkdir()
    monkeypatch.delenv("AGENT_TEST_A", raising=False)
    monkeypatch.setenv("AGENT_TEST_B", "from-shell")
    assert load_env(tmp_path / "sub") == tmp_path / ".env"           # found in a parent folder
    import os
    assert os.environ["AGENT_TEST_A"] == "from-file" and os.environ["AGENT_TEST_B"] == "from-shell"
    monkeypatch.delenv("AGENT_TEST_A")


# ------------------------------------------------------------------------------------------- rules
RULED_HEAT = {"op": "heat", "celsius": 40, "hold_s": 20, "rules": [
    {"when": {"value": "grams", "lt": 10}, "then": "skip"},
    {"when": {"value": "code", "starts_with": "10"}, "then": {"celsius": 80}}]}


def ruled_spec(steps):
    return {"name": "r", "summary": "r", "processes": [{"id": "p", "source": {"bay": 3, "rows": "C"},
                                                        "steps": steps, "destination": {"bay": 8}}]}


def test_rules_decide_per_vial_and_the_first_match_wins():
    from agent.spec import decide
    heat = WorkflowSpec.model_validate(ruled_spec([{"op": "scan"}, {"op": "weigh"}, RULED_HEAT])).processes[0].steps[2]
    assert decide(heat, {"grams": 8.2, "code": "10-A"})[0] is None                  # light: skipped, rule 1 first
    assert decide(heat, {"grams": 12.0, "code": "10-A"})[0].celsius == 80           # code 10...: hotter
    assert decide(heat, {"grams": 12.0, "code": "RK-A"}) == (heat, None)            # neither: as written
    assert decide(heat, {"grams": 12.0, "code": None})[0].celsius == 40             # no code read: no match


@pytest.mark.parametrize("steps, error", [
    ([{"op": "scan"}, RULED_HEAT], "only a weigh step measures"),                    # grams, but no weigh before
    ([{"op": "weigh"}, {"op": "scan"}, RULED_HEAT], None),                           # order of earlier steps is free
    ([{"op": "scan"}, {"op": "weigh"}, {**RULED_HEAT, "rules": [
        {"when": {"value": "grams", "lt": 10, "gt": 1}, "then": "skip"}]}], "exactly one comparison"),
    ([{"op": "scan"}, {"op": "weigh"}, {**RULED_HEAT, "rules": [
        {"when": {"value": "grams", "starts_with": "1"}, "then": "skip"}]}], "only code is text"),
    ([{"op": "scan"}, {"op": "weigh"}, {**RULED_HEAT, "rules": [
        {"when": {"value": "grams", "lt": 10}, "then": {"celsius": 150}}]}], "less than or equal to 100"),
    ([{"op": "weigh", "rules": [{"when": {"value": "read", "eq": False}, "then": "skip"}]}], "only a scan step"),
])
def test_the_spec_checks_rules(steps, error):
    if error is None:
        WorkflowSpec.model_validate(ruled_spec(steps))
    else:
        with pytest.raises(ValidationError, match=error):
            WorkflowSpec.model_validate(ruled_spec(steps))


# ------------------------------------------------------------------------------------------- example requests
def test_every_example_request_file_is_well_formed():
    from agent.examples import all_examples, find
    examples = all_examples()
    assert len(examples) >= 10 and len({e.name for e in examples}) == len(examples)
    assert any(e.expect_plan for e in examples) and any(not e.expect_plan for e in examples)
    assert all(e.request and "#" not in e.request for e in examples)          # notes are never sent
    assert find("3").name == find("03_rules").name == "03_rules_weight_and_code"


def test_an_example_file_needs_an_expect_line(tmp_path):
    from agent.examples import find
    (tmp_path / "x.txt").write_text("# a note\nScan row A of bay 3.\n")
    with pytest.raises(ValueError, match="expect"):
        find(str(tmp_path / "x.txt"))


def test_an_answer_continues_the_conversation():
    """Claude asks for a temperature; the person's answer is sent with the earlier turns, and Claude plans."""
    asking = ScriptedClient([[("rack_inventory", {"bay": 3})]], reply="Which temperature, 25 to 100 C?")
    first = plan_workflow("Heat row E of bay 3, then bay 8.", DemoInventory(), client=asking)
    assert first.spec is None and first.conversation == [
        {"role": "user", "content": "Heat row E of bay 3, then bay 8."},
        {"role": "assistant", "content": "Which temperature, 25 to 100 C?"}]

    heat_e = {"version": 1, "name": "heat_e", "summary": "Row E to 50 C.", "processes": [
        {"id": "heat", "source": {"bay": 3, "rows": "E"}, "steps": [{"op": "heat", "celsius": 50, "hold_s": 10}],
         "destination": {"bay": 8}}]}
    planning = ScriptedClient([[("submit_workflow", heat_e)], [("mark_plan_ready", {"note": "Row E to 50 C."})]],
                              reply="Row E will be heated to 50 C.")
    second = plan_workflow("50 C, hold 10 s", DemoInventory(), history=first.conversation, client=planning)
    assert planning.sent["messages"] == [*first.conversation, {"role": "user", "content": "50 C, hold 10 s"}]
    assert second.spec.processes[0].steps[0].celsius == 50 and second.ready and len(second.conversation) == 4


# ------------------------------------------------------------------------------------------- ready to run
def test_mark_plan_ready_needs_an_accepted_spec_and_a_new_submission_clears_it():
    box = PlanBox()
    tools = {t.name: t for t in make_tools(DemoInventory(), box)}
    with pytest.raises(Exception, match="submit_workflow first"):
        tools["mark_plan_ready"].call({"note": "too early"})
    tools["submit_workflow"].call(GOOD_SPEC)
    assert not box.ready
    assert "ready for approval" in tools["mark_plan_ready"].call({"note": "48 vials into bay 8."})
    assert box.ready and box.ready_note == "48 vials into bay 8."
    tools["submit_workflow"].call(GOOD_SPEC)                                     # a new spec ...
    assert not box.ready                                                         # ... must be marked again


def test_an_accepted_spec_that_is_not_marked_ready_is_not_ready():
    client = ScriptedClient([[("submit_workflow", GOOD_SPEC)]],
                            reply="Planned, but should the heated rows go to bay 8 or bay 7?")
    result = plan_workflow("...", DemoInventory(), client=client)
    assert result.spec is not None and not result.ready


# ------------------------------------------------------------------------------------------- sources
def source(**kw):
    from agent.spec import Source
    return Source(bay=3, **kw)


def test_a_source_is_rows_columns_or_chosen_wells():
    assert source(columns=[1, 12]).positions()[:3] == ["A1", "B1", "C1"] and len(source(columns=[1, 12]).positions()) == 16
    assert source(wells=["a6", "B6"]).positions() == ["A6", "B6"]                     # normalised to upper case
    assert source(rows="C").describe() == "rows C" and source(columns=[6]).describe() == "columns 6"
    assert source(columns=[6]).groups()[0] == ("column 6", [f"{r}6" for r in "ABCDEFGH"])


@pytest.mark.parametrize("kw, error", [
    ({}, "exactly one"), ({"rows": "C", "columns": [1]}, "exactly one"), ({"columns": [13]}, "columns are 1 to 12"),
    ({"columns": [2, 2]}, "each once"), ({"wells": ["I1"]}, "A1 to H12"), ({"wells": ["A1", "A1"]}, "each once"),
])
def test_a_source_refuses_what_is_not_a_set_of_wells(kw, error):
    with pytest.raises(ValidationError, match=error):
        source(**kw)


def test_a_swap_is_rejected_until_claude_adds_transit_wells():
    """Swap rows A and H of bay 3: the first submission has no transit wells and comes back with the cycle error;
    the second names twelve free wells (bay 8 row A) and is accepted."""
    swap = {"name": "swap_a_h", "summary": "Swap rows A and H of bay 3.", "processes": [
        {"id": "a_to_h", "source": {"bay": 3, "rows": "A"}, "steps": [], "destination": {"bay": 3, "rows": "H"}},
        {"id": "h_to_a", "source": {"bay": 3, "rows": "H"}, "steps": [], "destination": {"bay": 3, "rows": "A"}}]}
    with_transit = {**swap, "transit": [{"bay": 8, "well": f"A{c}"} for c in range(1, 13)]}
    client = ScriptedClient([
        [("rack_inventory", {"bay": 3}), ("rack_inventory", {"bay": 8})],
        [("submit_workflow", swap)],
        [("submit_workflow", with_transit)],
        [("mark_plan_ready", {"note": "Rows A and H of bay 3 swap places, via bay 8 row A."})],
    ])
    result = plan_workflow("swap the first row of vials with the last row in bay 3", DemoInventory(), client=client)
    errors = [(text, err) for _, text, err in client.results]
    assert errors[2][1] and "form a cycle" in str(errors[2][0]) and "add 12 more to transit" in str(errors[2][0])
    assert result.ready and len(result.report.routes) == 12
    assert result.report.moves["a_to_h"][0] == ("A1", "H1")


def test_a_spec_with_routes_is_accepted():
    spec = {"name": "sort_c", "summary": "Weigh row C; over 1 g to bay 8, the rest back.", "processes": [
        {"id": "sort", "source": {"bay": 3, "rows": "C"}, "steps": [{"op": "weigh"}],
         "routes": [{"when": {"value": "grams", "gt": 1}, "then": {"bay": 8}}], "destination": {"bay": 3}}]}
    client = ScriptedClient([[("submit_workflow", spec)], [("mark_plan_ready", {"note": "sort row C"})]])
    result = plan_workflow("weigh row C; heavier than 1 g to bay 8, otherwise back", DemoInventory(), client=client)
    assert result.ready and result.spec.processes[0].routes[0].then.bay == 8
    assert len(result.report.candidates) == 12


def test_the_system_prompt_keeps_claude_to_workcell_planning():
    assert "## Only workcell planning" in SYSTEM_PROMPT and "light-hearted line" in SYSTEM_PROMPT
