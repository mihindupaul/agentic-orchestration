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
  ui.py          the window: Describe > Plan > Run (.venv-orca/bin/python -m agent.ui, or make agent-ui)
  prompts/       the system prompt
  scheduler/     Orca's driver plug-in for the workcell, and Orca embedded in this process (needs Orca)

Needs the `agent` extra (anthropic, pydantic) and API credentials; the tests need neither.
"""

# Loaded on first use, so `agent.scheduler.core` (Orca- and SDK-free) imports without the agent extra.
_EXPORTS = {"plan_workflow": "planner", "PlanResult": "planner", "PlanningError": "planner",
            "WorkflowSpec": "spec", "ControllerInventory": "inventory", "DemoInventory": "inventory"}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name in _EXPORTS:
        from importlib import import_module
        return getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
