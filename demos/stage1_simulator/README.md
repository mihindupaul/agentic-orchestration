# Stage 1: the simulator and the controller

An imaginary sample-handling workcell, designed from scratch for learning, that you can talk to like real hardware, plus
the controller layer that knows how the cell is laid out.

```
 Mac                                                         ROS2 VM
 workcell_cli.py  "pick 3 A1"                                 simulator + RViz
      |
 vialctl.operations   pick = move, open, lower, grip, check, lift
 vialctl.deck         bay 3 A1 -> (364.5, 238.5)      --TCP-->  arm: MovL(364.5,238.5,20,0)  ToolDO(1,1)
                      scanner  -> (-170, 230)                   scanner: TRIG
```

The devices stay simple: every device listens on its own TCP port and speaks a short text protocol, and none of
them knows about bays, wells or stations. The arm only takes coordinates. Everything that knows the cell lives on
the Mac in `src/vialctl`: the deck file [`decks/workcell.json`](../../src/vialctl/decks/workcell.json) (where each
bay, well and station is, and the safe, grip and dock heights) and the labware operations built on it.

| Device | Port | Example commands |
|---|---|---|
| Arm dashboard | 29999 | `EnableRobot()`, `RobotMode()`, `GetPose()`, `ToolDI(1)` |
| Arm motion | 30003 | `MovL(x,y,z,r)`, `MovJ(x,y,z,r)`, `ToolDO(1,1)` (close gripper), `Sync()` |
| Arm feedback | 30004 | streams JSON state 10x per second |
| Balance | 5001 | `Z` (tare), `SI` (now), `S` (when stable) |
| Scanner | 5002 | `STATUS`, `TRIG` |
| Heater | 5003 | `STATUS`, `SET,<celsius>` (25-100), `OFF`, `TEMP` -> `T,<current>,<target>,<OFF\|HEATING\|STABLE>` |
| Admin (sim only) | 9000 | JSON: `{"cmd":"load_rack",...}`, `{"cmd":"state"}`, `{"cmd":"inject","fault":"scanner_noread"}` |

## Run it
Simulator and RViz in a container (needs Docker: OrbStack or Docker Desktop), everything else on the Mac:
```
python3 -m venv .venv && source .venv/bin/activate
make install
make sim-up                                         # start simulator + RViz: rack in bay 3 full, bay 8 empty
make sim-view                                       # the 3D view in the browser
python demos/stage1_simulator/workcell_cli.py       # the CLI talks to localhost
make sim-record SECS=60 NAME=take1                  # optional: 1080p clip in ros2/docker/recordings/
make sim-down
```
Other ways, same CLI:
- Without Docker: `make sim` runs the simulator alone, no 3D view.
- In the UTM VM (see [ros2/vm/README.md](../../ros2/vm/README.md)): `ros2/vm/deploy.sh`, then
  `python demos/stage1_simulator/workcell_cli.py --host 192.168.64.4`.

## Commands
`workcell_cli.py` is an interactive shell on top of the controller's operations. Each command prints the wire
lines the controller sends and the replies, so you see a name like `3 A1` turn into coordinates:
```
workcell> enable
workcell> raw arm GetPose()  # any device, any line, exactly as typed
workcell> move scanner
workcell> pick 3 A1          # move, lower, grip, check, raise
workcell> place scanner      # lower into the code reader, keep holding
workcell> scan
workcell> place heater       # carry it on to the heated well, keep holding
workcell> heat 60            # starts heating and returns at once
workcell> temp               # HEATING ... then STABLE once it has held 60 C for 3 s
workcell> heater off
```
`help` lists everything: arm power, motion (`move`, `down`, `up`, `grip`, `release`), vials (`pick`, `place`),
instruments (`scan`, `weigh`, `tare`, `heat`, `temp`, `heater off`), raw access, and simulator setup (`load`, `fault`).
`run demos/stage1_simulator/basics.txt` goes through each device on its own; `-c "enable; move home"` runs a
few commands and exits. `--deck` points the CLI at another deck file.

## Poke it by hand
```
nc 127.0.0.1 29999
EnableRobot()
MovL(0,300,100,0)
GetPose()
```
Things worth trying on camera: a move that is out of reach (`raw arm MovL(900,0,100,0)`), `scan` with no vial in
the scanner, `fault gripper_miss` followed by `pick 3 A1`, or a heater fault before `heat 60`.

Faults (`fault <name>`, one-shot, consumed by the next matching command):

| Fault | Effect |
|---|---|
| `scanner_noread` | next scan returns `NOREAD` |
| `balance_unstable` | next stable-weight request times out |
| `gripper_miss` | next gripper close grabs nothing |
| `gripper_slip` | next successful grip drops the vial a moment later |
| `arm_collision` | next move ends in a collision error |
| `heater_timeout` | next `SET` plateaus 5 C short of the target and never becomes `STABLE` |
| `heater_overshoot` | next `SET` overshoots by 8 C, then settles |

## Done when
- [x] Simulated devices on their own ports, fault injection, tests passing (`make test`)
- [x] Controller layer on the Mac: deck file and labware operations (`src/vialctl/deck.py`, `operations.py`),
  checked against the simulated cell (`tests/ctl/test_deck.py`)
- [x] Command line for every device, plain and raw (`workcell_cli.py`)
- [x] Runs from a fresh clone with the commands above
- [x] Talks to the simulator only over the network (`--host <vm-ip>`)
- [x] ROS2 VM running the simulator, reachable from the Mac (see [ros2/vm/README.md](../../ros2/vm/README.md))
- [x] RViz scene: arm from URDF, deck, racks, vials, stations, device activity
- [x] One command starts simulator + RViz (`make sim-up`; or `ros2/vm/deploy.sh` for the VM)
- [ ] `POST.md` outline written, session recorded, posts cut (see [CONTENT.md](../CONTENT.md))
- [ ] Repo on GitHub, tagged `stage1-simulator`
