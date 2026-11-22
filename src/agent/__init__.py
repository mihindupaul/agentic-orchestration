"""The planning agent: a person describes vial work in plain words, the agent turns it into a workflow
that the cell can run.

The model only ever produces data, a WorkflowSpec. Plain code does everything that has to be right:

    request ──> planner (Claude + tools) ──> WorkflowSpec ──> validator ──> compiler ──> approval ──> run
                 step 1                         spec.py        step 3       step 2      step 4

  spec.py        the WorkflowSpec: the only shape a plan can take (processes, steps from a fixed menu, rules)
  inventory.py   where the deck state comes from: the live controller, or a fixed demo deck
  tools.py       what Claude may call while planning: describe_cell, rack_inventory, submit_workflow,
                 mark_plan_ready
  planner.py     the Claude API call (the SDK's tool runner) and the checks on its result
  compiler.py    WorkflowSpec -> Orca topology + workflow (needs Orca); rules are evaluated here, per vial
  prompts/       the system prompt

    python -m agent "scan and weigh rows C and E of bay 3, then put them in bay 8" --offline

Needs the `agent` extra (anthropic, pydantic) and API credentials; the tests need neither.
"""

from .inventory import ControllerInventory, DemoInventory
from .planner import PlanningError, PlanResult, plan_workflow
from .spec import WorkflowSpec

__all__ = ["plan_workflow", "PlanResult", "PlanningError", "WorkflowSpec", "ControllerInventory", "DemoInventory"]
