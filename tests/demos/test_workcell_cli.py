"""The stage 1 CLI drives a live simulator through the same TCP ports a viewer would use."""

import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "demos", "stage1_simulator"))

from workcell_sim import Simulator, Timing  # noqa: E402
from workcell_sim.simulator import load_demo_racks  # noqa: E402
from vialctl.operations import Workcell  # noqa: E402
from workcell_cli import WorkcellShell  # noqa: E402

BASICS = os.path.join(os.path.dirname(__file__), "..", "..", "demos", "stage1_simulator", "basics.txt")
PORT_NAMES = ("arm_dashboard", "arm_motion", "arm_feedback", "balance", "scanner", "heater", "admin")


def _start_sim():
    sim = Simulator(timing=Timing(time_scale=5000.0))
    load_demo_racks(sim.world)
    loop = asyncio.new_event_loop()
    ports = {}
    ready = threading.Event()

    async def boot():
        ports.update(await sim.start(ports={k: 0 for k in PORT_NAMES}))
        ready.set()

    threading.Thread(target=lambda: (loop.create_task(boot()), loop.run_forever()), daemon=True).start()
    ready.wait(5)
    return sim, loop, ports


def _shell(ports, trace=lambda *a: None):
    """A CLI shell with its own event loop, connected like a viewer's would be."""
    loop = asyncio.new_event_loop()
    cell = loop.run_until_complete(Workcell.connect("127.0.0.1", ports, trace=trace))
    return WorkcellShell(cell, loop)


def _close(shell):
    shell.loop.run_until_complete(shell.cell.close())
    shell.loop.close()


def _stop_sim(sim, loop):
    async def shutdown():
        await sim.stop()
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run_coroutine_threadsafe(shutdown(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def test_basics_script_runs_device_by_device():
    sim, loop, ports = _start_sim()
    wire, said = [], []
    shell = _shell(ports, trace=lambda *a: wire.append(a))
    try:
        shell.say = said.append
        shell.onecmd(f"run {BASICS}")
        replies = {text: reply for _dev, text, reply in wire}
        assert replies["TRIG"] == "ERR,NO_VIAL" and replies["SET,60"] == "ERR,NO_VIAL"
        assert "holding a vial" in said and said[-1] == "arm disabled"
        assert (0, 0) in sim.world.racks[3].wells and sim.world.held is None     # vial went back to A1
    finally:
        _close(shell)
        _stop_sim(sim, loop)


def test_scan_one_vial_by_hand():
    sim, loop, ports = _start_sim()
    said = []
    shell = _shell(ports)
    try:
        shell.say = said.append
        for line in ("enable", "pick 3 A1", "move scanner", "place scanner", "scan", "pick scanner", "place 3 A1"):
            shell.onecmd(line)
        assert "code: RACK003-A1" in said and sim.world.held is None
    finally:
        _close(shell)
        _stop_sim(sim, loop)


def test_errors_are_reported_not_raised():
    sim, loop, ports = _start_sim()
    said = []
    shell = _shell(ports)
    try:
        shell.say = said.append
        shell.onecmd("enable")
        shell.onecmd("pick 9 A1")                     # empty rack: reported, shell keeps going
        shell.onecmd("raw arm GetPose()")
        assert sim.world.held is None and said[-1] == "arm enabled"
    finally:
        _close(shell)
        _stop_sim(sim, loop)


def test_heat_temp_and_heater_off():
    sim, loop, ports = _start_sim()
    said = []
    shell = _shell(ports)
    try:
        shell.say = said.append
        for line in ("enable", "heat 60", "pick 3 A1", "place heater", "heat 120", "heat 60", "temp"):
            shell.onecmd(line)
        assert said[-2].startswith("heating to 60 C, now ") and "HEATING" in said[-1]
        shell.loop.run_until_complete(asyncio.sleep(0.05))        # ~250 simulated seconds at 5000x
        shell.onecmd("temp")
        assert said[-1] == "heater 60.0 C, target 60 C, STABLE"
        shell.onecmd("heater off")
        shell.onecmd("temp")
        assert said[-2] == "heater off" and said[-1].endswith("off, OFF")
        shell.onecmd("heater on")                                  # usage error: reported, not raised
        assert said[-1].endswith("off, OFF")
        assert sim.heater.target is None and sim.world.in_heater.peak_c >= 59.5   # set down in the well
    finally:
        _close(shell)
        _stop_sim(sim, loop)
