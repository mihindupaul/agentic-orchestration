# The workcell controller

The devices are deliberately simple: every device listens on its own TCP port and speaks a short text protocol, and
none of them knows about bays, wells or stations. The arm only takes coordinates. Everything that knows the cell
lives here, on the Mac: the deck file [`decks/workcell.json`](decks/workcell.json) (where each bay, well and
station is, and the safe, grip and dock heights) and the labware operations built on it.

```
 Mac                                                         simulator (container or VM)
 "pick 3 A1"
      |
 operations.py   pick = move, open, lower, grip, check, lift
 deck.py         bay 3 A1 -> (364.5, 238.5)      --TCP-->  arm: MovL(364.5,238.5,20,0)  ToolDO(1,1)
                 scanner  -> (-170, 230)                   scanner: TRIG
```

| File | What it does |
|---|---|
| `deck.py`, `decks/workcell.json` | bay, well and station names -> coordinates and heights |
| `devices.py` | the wire clients, one per device (optional `trace` callback: every line sent and its reply) |
| `operations.py` | `Workcell`: pick, place, scan, weigh, heat ... by name |
| `controller.py` | the service: one owner of the hardware, one operation at a time, a fault latch |
| `api.py`, `__main__.py` | the service's JSON-lines API (`make ctl`, port 8700) and its one-shot client |
| `cli.py` | an interactive shell straight to the devices, showing every wire line (`make cli`) |

## Two ways in

**The controller service** is what Orca and the planning agent use: one owner of the hardware, so two clients can
never move the arm at the same time.
```
make ctl SIM_HOST=192.168.64.4                       # the service, API on :8700
python -m workcell_controller status                 # in another terminal
python -m workcell_controller do pick 3 A1
python -m workcell_controller do place scanner
python -m workcell_controller do scan
```

**The workcell shell** talks to the devices directly and prints each line it sends with the reply, so you see a
name like `3 A1` turn into coordinates. Stop `make ctl` first: the shell does not go through the service.
```
make cli SIM_HOST=192.168.64.4                       # or: python -m workcell_controller.cli --host 192.168.64.4
workcell> enable
workcell> raw arm GetPose()  # any device, any line, exactly as typed
workcell> move scanner
workcell> pick 3 A1          # move, lower, grip, check, raise
workcell> place scanner      # lower into the code reader, keep holding
workcell> scan
workcell> place heater       # set it down in the heated well (the arm is free while it heats)
workcell> heat 60            # starts heating and returns at once
workcell> temp               # HEATING ... then STABLE once it has held 60 C for 3 s
workcell> heater off
```
`help` lists everything: arm power, motion (`move`, `down`, `up`, `grip`, `release`), vials (`pick`, `place`),
instruments (`scan`, `weigh`, `tare`, `heat`, `temp`, `heater off`), raw access, and simulator setup (`load`,
`fault`). `run src/workcell_controller/cli_basics.txt` goes through each device on its own; `-c "enable; move home"`
runs a few commands and exits. `--deck` points the shell at another deck file.

## The devices

| Device | Port | Example commands |
|---|---|---|
| Arm dashboard | 29999 | `EnableRobot()`, `RobotMode()`, `GetPose()`, `ToolDI(1)` |
| Arm motion | 30003 | `MovL(x,y,z,r)`, `MovJ(x,y,z,r)`, `ToolDO(1,1)` (close gripper), `Sync()` |
| Arm feedback | 30004 | streams JSON state 10x per second |
| Balance | 5001 | `Z` (tare), `SI` (now), `S` (when stable) |
| Scanner | 5002 | `STATUS`, `TRIG` |
| Heater | 5003 | `STATUS`, `SET,<celsius>` (25-100), `OFF`, `TEMP` -> `T,<current>,<target>,<OFF\|HEATING\|STABLE>` |
| Admin (sim only) | 9000 | JSON: `{"cmd":"load_rack",...}`, `{"cmd":"state"}`, `{"cmd":"inject","fault":"scanner_noread"}` |

Any of them by hand:
```
nc 127.0.0.1 29999
EnableRobot()
MovL(0,300,100,0)
GetPose()
```

## Faults
Worth trying: a move that is out of reach (`raw arm MovL(900,0,100,0)`), `scan` with no vial in the scanner,
`fault gripper_miss` followed by `pick 3 A1`, or a heater fault before `heat 60`. Faults (`fault <name>` in the
shell) are one-shot, consumed by the next matching command:

| Fault | Effect |
|---|---|
| `scanner_noread` | next scan returns `NOREAD` |
| `balance_unstable` | next stable-weight request times out |
| `gripper_miss` | next gripper close grabs nothing |
| `gripper_slip` | next successful grip drops the vial a moment later |
| `arm_collision` | next move ends in a collision error |
| `heater_timeout` | next `SET` plateaus 5 C short of the target and never becomes `STABLE` |
| `heater_overshoot` | next `SET` overshoots by 8 C, then settles |
