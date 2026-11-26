# Architecture

**Simulator** (`workcell_sim`): world model (racks, vial, balance pan), queued-motion SCARA arm with IK,
balance, scanner and heater servers, fault injection, deterministic time scaling.

**Devices stay simple.** The simulated arm takes x, y, z moves and gripper open/close; balance, scanner and
heater read or heat whatever is in front of them. No device knows about bays, wells or stations.

**Controller** (`workcell_controller`, on the Mac), in layers:
- `deck.py` + `decks/workcell.json`: the deck. Bay and well addresses and station names become arm coordinates;
  safe, grip and dock heights. On real hardware these are taught positions.
- `operations.py`: labware operations built from device commands: `pick("3 A1")`, `place("scanner")` (docks and
  keeps holding), `place("balance")` (sets down), `scan`, `weigh`, `heat` (starts heating, returns at once), `temperature`, `heater_off`. The workcell shell (`make cli`, `cli.py`) drives these directly.
- `controller.py`: the service. The one owner of the hardware: one operation at a time (a second request is
  refused, not queued), and a fault latch (arm fault, lost reply or lost vial: everything is refused until an
  operator calls `clear_fault`). It makes no decisions about samples.
- `api.py`: newline JSON on :8700. Operations `home pick place scan weigh tare heat temperature heater_off rack_map`, plus `ping status
  events clear_fault emergency_stop`. Example: `{"cmd": "pick", "where": "3 A1"}` -> `{"ok": true, "at": "bay 3 A1"}`.

**Rack jobs are workflows, not controller code.** Which tubes, in what order, how many scan retries, what to do
with a tube that will not read: an Orca workflow composes these from the controller's operations.

**Orca plug-in** (`orca_workcell`, stage 2, in progress): one Orca action per controller operation. Results a
workflow can branch on (no-read, heater not yet stable, empty well) come back as data; busy/fault -> refused, bad request
-> rejected, lost vial or arm fault -> failed, lost reply -> outcome unknown. Rack-placed/picked hooks tell the
simulator a rack arrived or left.
