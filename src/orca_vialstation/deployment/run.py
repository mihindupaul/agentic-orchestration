"""Run a workflow of this deployment package in-process.

    python -m orca_vialstation.deployment.run --mode sim [workflow] [--var name=value ...]
    python -m orca_vialstation.deployment.run --mode live driver_check --var heat_c=50   # needs sim-up + ctl

Each workflow lives in workflows/<name>.py and leaves what it found in that module's `results`.

In a live run every thread that starts with MANUAL_PLACE waits for its rack to be registered at its bay.
This script does that registration itself, standing in for the operator who would otherwise confirm it in
Orca after putting the rack down (`--no-auto-place` leaves it to them).
"""

import argparse
import asyncio
import importlib
import json
import logging
import time

import orca.orca as orca
from orca.runtime.execution_phase import ExecutionPhase
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime

log = logging.getLogger("orca")


async def _place_when_asked(runtime: SystemRuntime, execution_id: str) -> None:
    """Operator stand-in: whenever a thread waits for its rack, register it where the thread wants it.

    This is the same step as the "I placed it" remedy Orca offers an operator (`orca labware register`)."""
    done: set = set()
    while True:
        for thread in runtime.threads.list(execution_id):
            if thread.status == "AWAITING_MANUAL_PLACE" and thread.id not in done:
                await runtime.labware.register(thread.labware_template_name, location=thread.waiting_for,
                                               confirm=True, reason="rack placed (run script)")
                log.info("registered %s at %s", thread.labware_template_name, thread.waiting_for)
                done.add(thread.id)
        await asyncio.sleep(0.1)


async def close_drivers(system) -> None:
    """Close the workcell drivers' connections. This Orca release does not call drivers' `_shutdown`
    when the runtime stops, and an open connection keeps the controller (and simulator) waiting."""
    for device in system.devices:
        for driver in {id(d): d for d in (device.live_driver, device.driver_under(WorkflowRunMode.PURE_SIM))}.values():
            await driver._shutdown()


def _value(text: str):
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(text.lower(), text)


PACKAGES = {"workcell": "orca_vialstation.deployment", "cell": "orca_vialstation.cell_deployment"}


def results_of(workflow: str, package: str = "workcell") -> dict:
    return importlib.import_module(f"{PACKAGES[package]}.workflows.{workflow}").results


async def run(workflow="one_vial", live: bool = False, auto_place: bool = True, variables: dict | None = None,
              sequential: bool = False, package: str = "workcell") -> float:
    """Run one workflow, or several (a list). Several are submitted to one Orca runtime together, so they
    run at the same time and Orca interleaves their actions on the shared device; `sequential=True` runs
    them one after the other instead. Variables a workflow does not declare are left out of its submission.
    Returns the elapsed wall-clock seconds."""
    names = [workflow] if isinstance(workflow, str) else list(workflow)
    build = await orca.build_system_from_package(PACKAGES[package], InMemoryRuntimeStoreFactory())
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    mode = WorkflowRunMode.LIVE if live else WorkflowRunMode.PURE_SIM
    placing: list = []
    await runtime.start()
    t0 = time.monotonic()
    try:
        async def submit(name):
            template = build.system.get_workflow_template(name)
            own = {k: v for k, v in (variables or {}).items() if k in template.variable_definitions} or None
            sub = await runtime.submit(template, variables=own, mode=mode)
            if live and auto_place:
                placing.append(asyncio.create_task(_place_when_asked(runtime, sub.execution_id)))
            return name, sub

        async def finish(name, sub):
            status = await runtime.wait_for_execution(sub)
            if status.status is not ExecutionPhase.COMPLETED:
                raise RuntimeError(f"{name} ended {status.status.value}: {status.error}")

        if sequential:
            for name in names:
                await finish(*await submit(name))
        else:
            submitted = [await submit(name) for name in names]
            await asyncio.gather(*(finish(name, sub) for name, sub in submitted))
    finally:
        for task in placing:
            task.cancel()
        await runtime.shutdown()
        await close_drivers(build.system)
    return time.monotonic() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workflows", nargs="*", default=["one_vial"], help="one or more; several run at the same time")
    ap.add_argument("--mode", choices=["sim", "live"], default="sim")
    ap.add_argument("--sequential", action="store_true", help="run several workflows one after the other")
    ap.add_argument("--package", choices=sorted(PACKAGES), default="workcell",
                    help="workcell: the cell as one Orca device; cell: arm, instruments and vials as Orca sees them")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE", help="workflow variable")
    ap.add_argument("--no-auto-place", action="store_true",
                    help="live runs: wait for an operator to register racks instead of doing it here")
    a = ap.parse_args()
    variables = {k: _value(v) for k, v in (item.split("=", 1) for item in a.var)}
    elapsed = None
    try:
        elapsed = asyncio.run(run(a.workflows, a.mode == "live", not a.no_auto_place, variables, a.sequential,
                                  a.package))
    finally:
        for name in a.workflows:
            print(f"--- {name}")
            print(json.dumps(results_of(name, a.package), indent=2))
        if elapsed is not None:
            how = "one after the other" if a.sequential else "at the same time"
            print(f"--- {len(a.workflows)} workflow(s) {how}: {elapsed:.1f} s")


if __name__ == "__main__":
    main()
