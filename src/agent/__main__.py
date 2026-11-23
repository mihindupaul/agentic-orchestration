"""Plan a request from the command line and print the spec; with --run, also run it on the cell.

    python -m agent "heat rows D and F of bay 3 to 50 C, then move them to bay 8" --offline
    python -m agent "..." --controller 127.0.0.1:8700        # deck state from the running controller
    python -m agent --list                                    # the example requests in examples/agent_requests/
    python -m agent --example 03 --offline                    # run one of them
    python -m agent "..." --controller 127.0.0.1:8700 --run   # plan, ask for approval, run on the cell

Each API call costs a little; --offline only replaces the deck (a fixed demo deck), not the model. Running
needs Orca (.venv-orca) and the controller; the model is not called while the plan runs.
"""

import argparse
import json
import sys
import time

from .env import load_env
from .examples import all_examples, find
from .inventory import ControllerInventory, DemoInventory
from .planner import PlanningError, plan_workflow


def show(message) -> None:
    """Print what Claude did in one round trip: its tool calls, as they happen."""
    for block in message.content:
        if block.type == "tool_use":
            print(f"  -> {block.name}({json.dumps(block.input)[:100]})", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m agent", description="Plan vial work as a workflow spec.")
    ap.add_argument("request", nargs="?", help="what should happen, in plain words")
    ap.add_argument("--example", metavar="N", help="use an example request from examples/agent_requests/ (number, name or .txt path)")
    ap.add_argument("--list", action="store_true", help="list the example requests and exit")
    ap.add_argument("--offline", action="store_true", help="use the demo deck (bay 3 full, bay 8 empty)")
    ap.add_argument("--controller", default="127.0.0.1:8700", help="controller host:port for the live deck")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--run", action="store_true", help="after planning, ask for approval and run the plan on the cell")
    ap.add_argument("--approve", action="store_true", help="with --run: the plan is approved already, do not ask")
    a = ap.parse_args()
    if a.list:
        for e in all_examples():
            print(f"{e.name:38} expects {'a plan' if e.expect_plan else 'no plan':8}  {e.request[:70]}")
        return
    expect = None
    if a.example:
        example = find(a.example)
        a.request, expect = example.request, example.expect_plan
        print(f"{example.name}: {example.request}\n", file=sys.stderr)
    if not a.request:
        ap.error("give a request, or --example N (see --list)")
    load_env()                                         # ANTHROPIC_API_KEY from .env, if not already set

    host, port = a.controller.rsplit(":", 1)
    inventory = DemoInventory() if a.offline else ControllerInventory(host, int(port))
    request, history = a.request, None
    while True:
        try:
            result = plan_workflow(request, inventory, history=history, effort=a.effort, on_message=show)
        except PlanningError as exc:
            sys.exit(f"planning failed: {exc}")
        print(result.reply, file=sys.stderr)
        if result.ready or expect is not None or not sys.stdin.isatty():
            break
        # Not ready: Claude asked something, explained a problem or wants a choice confirmed. Answer it to
        # continue the same conversation.
        request = input("\nyour answer (Enter to stop): ").strip()
        if not request:
            break
        history = result.conversation

    if expect is not None:
        got = result.ready
        print(f"\n[{'as expected' if got == expect else 'NOT as expected'}: "
              f"{'a plan' if got else 'no plan'}, the example expects {'a plan' if expect else 'no plan'}]",
              file=sys.stderr)
    if result.spec is None:
        sys.exit(1)                                    # Claude did not submit; its reply says why
    print(result.spec.model_dump_json(indent=2, exclude_none=True))     # unset comparisons left out
    if not result.ready:
        sys.exit("the plan is not marked ready to run: Claude is waiting for an answer")
    if a.run:
        if a.offline:
            sys.exit("--run needs the live deck: leave out --offline")
        execute(result.spec, inventory, host, int(port), a.approve)


def execute(spec, inventory, host: str, port: int, approved: bool) -> None:
    """Approval, a last check against the deck, then the compiled plan on the cell. No model calls from here."""
    import asyncio

    from orca_workcell.core import ControllerConfig

    from .compiler import compile_spec, run
    from .validator import validate

    report = validate(spec, inventory)                 # the deck may have changed since planning
    print(f"\nplan {spec.name}: {spec.summary}", file=sys.stderr)
    for pid, wells in report.vials.items():
        print(f"  {pid}: {len(wells)} vials", file=sys.stderr)
    print(report.text(), file=sys.stderr)
    if not report.ok:
        sys.exit("not run: the deck no longer fits the plan")
    if not approved and input("Run this plan on the cell? [y/N] ").strip().lower() != "y":
        sys.exit("not run")
    plan = compile_spec(spec, ControllerConfig(ctl_host=host, ctl_port=port), report.vials)
    clock = {}
    results = asyncio.run(run(plan, on_state=lambda s: clock.setdefault(s, time.monotonic())))
    print(json.dumps(results, indent=1))
    counts = {}
    for record in results.values():
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    print(f"\n{len(results)} vials: " + ", ".join(f"{n} {status}" for status, n in sorted(counts.items())),
          file=sys.stderr)
    elapsed = clock.get("finished", time.monotonic()) - clock.get("running", time.monotonic())
    print(f"run time {int(elapsed // 60)}:{int(elapsed % 60):02d} ({elapsed:.0f} s), "
          f"{len(results) / (elapsed / 60):.1f} vials/min (Orca build time not included)", file=sys.stderr)


if __name__ == "__main__":
    main()
