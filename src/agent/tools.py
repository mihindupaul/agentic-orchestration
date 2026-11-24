"""The tools the planning agent may use.

A tool is an ordinary Python function. `@beta_tool` (from the Anthropic SDK) turns it into a tool definition
that is sent to Claude with every request: the function name becomes the tool name, the docstring becomes the
description Claude reads to decide when to use it, and the parameters (or a Pydantic model) become the input
schema. Claude never runs anything itself: it asks for a tool call, the SDK's tool runner calls the function,
and the return value (or the error) goes back to Claude.

Step 1 tools (planning only, nothing moves):
  describe_cell     what the cell has and what the steps can do
  rack_inventory    which wells of a bay hold vials right now
  submit_workflow   hand in the finished spec; it is checked against the schema, then against the deck
                    (validator.py), before it is accepted
  mark_plan_ready   say the accepted spec is the plan to run: nothing left for the person to decide. Only then
                    does the application offer the plan for approval and the run
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from anthropic.lib.tools import ToolError, beta_tool
from pydantic import ValidationError

from .inventory import Inventory
from .spec import COLUMNS, ROW_LETTERS, WorkflowSpec
from .validator import Report, validate

CELL = {
    "rack_bays": {"bays": list(range(1, 9)), "rows": ROW_LETTERS, "columns": f"{COLUMNS.start}-{COLUMNS.stop - 1}",
                  "wells_per_rack": len(ROW_LETTERS) * len(COLUMNS)},
    "instruments": {
        "code_reader": "reads the 2D code of one vial (step: scan)",
        "balance": "weighs one vial (step: weigh)",
        "heater": "heats one vial to 25-100 C and holds it (step: heat)",
    },
    "arm": "moves one vial at a time between rack wells and instruments; the scheduler does the moves",
    "parallel": "processes run at the same time; while a vial heats, the arm and the other instruments serve "
                "other vials. Each instrument holds one vial at a time.",
    "steps": {
        "scan": {"tries": "1-10, default 3", "measures": "code (text, missing if unread), read (true/false)"},
        "weigh": {"measures": "grams"},
        "heat": {"celsius": "25-100", "hold_s": "0-3600 seconds at temperature", "measures": "celsius"},
    },
    "rules": "any step can carry rules: per-vial decisions on a value an earlier step of the same process "
             "measured. Tests: lt, le, gt, ge (grams, celsius), eq (any value), starts_with, ends_with (code). "
             "Outcomes: skip the step, or run it with changed values (scan: tries; heat: celsius, hold_s; "
             "weigh: skip only). The first rule that holds decides. A skipped step is decided "
             "before the vial moves, so the vial does not go to that instrument at all.",
    "sources": "a process takes vials from one bay: whole rows (row letters), whole columns (numbers 1-12) or "
               "chosen wells (e.g. A6, B6, C6, D6 for the first four vials of column 6)",
    "destinations": "a process puts its vials into one bay, which may be the source bay: the same well "
                    "positions (default), other rows (one row letter per source row, the column stays), other "
                    "columns (one number per source column, the row stays) or chosen wells (one per source well, "
                    "paired in order). A destination well must be free, or hold a vial the plan moves away. A "
                    "process with no steps is a plain move: the arm takes each vial straight to its destination",
    "transit": "moves that form a cycle (a swap: row A to row H while row H goes to row A) need a free well "
               "where one vial of each cycle waits: one transit well per cycle (12 for a swap of two full rows). "
               "Choose them from rack_inventory: wells that are empty and that no vial of the plan is taken "
               "from or put into. A chain (A to B while B goes to a free C) needs none",
}


@dataclass
class PlanBox:
    """Where submit_workflow leaves the accepted spec, for the code that started the agent."""
    spec: WorkflowSpec | None = None
    report: Report | None = None                      # the validator's findings on the accepted spec
    attempts: list = field(default_factory=list)      # every submission, accepted or not
    ready: bool = False                               # mark_plan_ready was called for the accepted spec
    ready_note: str = ""                              # what Claude said when it marked the plan ready


def make_tools(inventory: Inventory, box: PlanBox) -> list:
    """Build the tool set. The tools close over `inventory` (where the deck state comes from) and `box`
    (where the result goes), so the same functions work against the live controller or the demo deck."""

    @beta_tool
    def describe_cell() -> str:
        """Describe the workcell: rack bays and wells, the instruments, the steps a vial can go through and
        their limits, and how processes share the hardware.

        Call this first, before planning.
        """
        return json.dumps(CELL, indent=1)

    @beta_tool
    def rack_inventory(bay: int) -> str:
        """List which wells of a rack bay hold vials right now.

        Use it for every bay a plan takes vials from or puts vials into: a plan may only pick from wells that
        hold vials and only place into wells that are free.

        Args:
            bay: Rack bay number, 1 to 8.
        """
        if not 1 <= bay <= 8:
            raise ToolError(f"there is no bay {bay}; bays are 1 to 8")
        rack = inventory.rack(bay)
        occupied = rack["occupied"]
        rows = {row: sum(w[0] == row for w in occupied) for row in ROW_LETTERS}
        columns = {col: sum(w[1:] == str(col) for w in occupied) for col in COLUMNS}
        return json.dumps({**rack, "count": len(occupied), "vials_per_row": rows, "vials_per_column": columns})

    def submit_workflow(**spec) -> str:
        box.attempts.append(spec)
        try:
            checked = WorkflowSpec.model_validate(spec)
        except ValidationError as exc:
            # Raised as a ToolError so Claude sees *which* field is wrong; any other exception would reach it
            # only as "Invalid arguments for function submit_workflow", with nothing to fix it by.
            raise ToolError("spec rejected:\n" + _readable(exc)) from exc
        report = validate(checked, inventory)                  # the deck as it is now
        if not report.ok:
            raise ToolError("the deck cannot run this plan:\n" + report.text())
        box.spec, box.report = checked, report
        box.ready, box.ready_note = False, ""                   # a new spec has to be marked ready again
        moved = ", ".join(f"{pid}: {len(wells)} vials" for pid, wells in report.vials.items())
        if report.routes:
            moved += f"; {len(report.routes)} vial(s) wait in a transit well"
        return f"accepted ({moved})" + ("".join(f"\nwarning: {w}" for w in report.warnings))

    # The input schema comes from the WorkflowSpec model; the SDK validates Claude's arguments against it before
    # the function runs, and a failure goes back to Claude as an error it can correct.
    submit = beta_tool(
        submit_workflow,
        input_schema=WorkflowSpec,
        description=(
            "Hand in the finished workflow spec. Call it once, when the plan is complete and every bay it uses "
            "has been checked with rack_inventory. The spec is checked against the schema and then against the "
            "deck (racks present, source wells holding vials, destination wells free or emptied by the plan, "
            "no two processes sharing wells, a free transit well for every cycle of moves); if it is rejected, "
            "fix what the error says and submit again. Warnings on acceptance (such as empty wells that will be "
            "skipped) belong in your reply to the person."
        ),
    )

    @beta_tool
    def mark_plan_ready(note: str) -> str:
        """Mark the accepted spec as the plan to run. The application offers a plan for approval and the run
        only after this call.

        Call it after submit_workflow has accepted the spec and you have checked it does what the person asked,
        with nothing left for them to decide. Do not call it when you still need an answer from the person (a
        choice between options, a value they must confirm): ask them instead. Submitting a spec again clears it.

        Args:
            note: One sentence for the person approving the plan: what will run.
        """
        if box.spec is None:
            raise ToolError("no accepted spec yet: call submit_workflow first")
        box.ready, box.ready_note = True, note
        return f"plan {box.spec.name} is ready for approval"

    return [describe_cell, rack_inventory, submit, mark_plan_ready]


def _readable(exc: ValidationError) -> str:
    """One line per problem, e.g. 'processes.0.steps.0.celsius: Input should be less than or equal to 100'."""
    return "\n".join(f"{'.'.join(str(p) for p in err['loc']) or 'spec'}: {err['msg']}" for err in exc.errors())
