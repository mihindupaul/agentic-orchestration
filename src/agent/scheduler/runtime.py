"""Orca embedded in this process: run a built system's workflow live, through the drivers to the controller.

No Orca daemon or device gateway is involved: the drivers are ordinary objects in this process (the Orca CLI's
live mode expects devices to connect to its daemon over Orca's own WebSocket protocol).

Every vial thread starts with MANUAL_PLACE: in a live run it waits until its vial is registered at its rack
well. `run_workflow` does that registration itself, standing in for the operator who would otherwise confirm
in Orca that the vials are in place.
"""

import asyncio
import logging
import time

from orca.runtime.execution_phase import ExecutionPhase
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime

log = logging.getLogger("orca")


async def _place_when_asked(runtime: SystemRuntime, execution_id: str) -> None:
    """Operator stand-in: whenever a thread waits for its vial, register it where the thread wants it.

    This is the same step as the "I placed it" remedy Orca offers an operator (`orca labware register`)."""
    done: set = set()
    while True:
        for thread in runtime.threads.list(execution_id):
            if thread.status == "AWAITING_MANUAL_PLACE" and thread.id not in done:
                await runtime.labware.register(thread.labware_template_name, location=thread.waiting_for,
                                               confirm=True, reason="vial in place")
                log.info("registered %s at %s", thread.labware_template_name, thread.waiting_for)
                done.add(thread.id)
        await asyncio.sleep(0.1)


async def close_drivers(system) -> None:
    """Close the drivers' controller connections: the instruments' and the arm's (Orca lists the arm under
    `transporters`, not `devices`). This Orca release does not call drivers' `_shutdown` when the runtime
    stops, and an open connection keeps the controller waiting."""
    for resource in [*system.devices, *system.transporters]:
        drivers = {id(d): d for d in (resource.live_driver, resource.driver_under(WorkflowRunMode.PURE_SIM))}
        for driver in drivers.values():
            shutdown = getattr(driver, "_shutdown", None)
            if shutdown is not None:
                await shutdown()


async def run_workflow(build, name: str, on_submitted=None) -> float:
    """Run workflow `name` of a built Orca system live, then close the drivers. `on_submitted(runtime,
    execution_id)` is called once it is submitted, e.g. for a window that shows its phase or stops it.
    Returns the elapsed wall-clock seconds."""
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    t0 = time.monotonic()
    placing = None
    try:
        sub = await runtime.submit(build.system.get_workflow_template(name), mode=WorkflowRunMode.LIVE)
        placing = asyncio.create_task(_place_when_asked(runtime, sub.execution_id))
        if on_submitted:
            on_submitted(runtime, sub.execution_id)
        status = await runtime.wait_for_execution(sub)
        if status.status is not ExecutionPhase.COMPLETED:
            raise RuntimeError(f"{name} ended {status.status.value}: {status.error}")
    finally:
        if placing:
            placing.cancel()
        await runtime.shutdown()
        await close_drivers(build.system)
    return time.monotonic() - t0
