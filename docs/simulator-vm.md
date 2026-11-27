# Simulator in ROS2 (container or VM), everything else on the Mac

## Boundary
```
 Container or VM (ROS2 + RViz)                    |  macOS host
                                                  |
 simulator (devices, world, physics)              |  controller     (the service, its device shell)
   sim_node: hosts the simulator, publishes ----> RViz  agent          (AI planning agent, its window)
   robot_state_publisher, markers, TF             |    scheduler    (Orca and its driver plug-in)
                                                  |
        device TCP ports 29999 30003 30004  <-----+----- only link between the two sides
                         5001 5002 5003 9000      |
```
- The network is the only interface. Code on the Mac never imports `simulator`; it gets the simulator
  address from configuration (`--host`, `--sim-host`, `ControllerConfig`) and talks to the device ports, as it would
  to real hardware. Real devices speaking the same protocols would only need a different IP address.
- The ROS2 side owns everything visual. RViz is the simulator's window; the Mac-side story (orchestration) does not
  depend on it.
- The simulator stays stdlib-only and runs headless anywhere (`python -m simulator`). Tests on the Mac
  start it in-process; that is test scaffolding, not a runtime dependency.

## Container (default)
`make sim-up` builds and starts `ros2/docker` (OrbStack or Docker Desktop); `make sim-view` opens the view.
- ROS2 Jazzy on arm64, the repo mounted read-only at `/repo` and built with colcon at start, so edits need only
  `make sim-down sim-up`.
- No GPU in containers on a Mac: RViz renders in software (Mesa llvmpipe, OpenGL 4.5) on a 1920x1080 virtual
  display. Measured on an M2 Pro: 31 fps idle and while the arm moves, about 1-1.5 CPU cores, under 800 MB.
- The browser view is noVNC (port 6080), which compresses the picture. For videos record the display itself:
  `make sim-record SECS=60 NAME=take1` writes a 30 fps 1080p clip to `ros2/docker/recordings/`.
- Device ports are published on `localhost`, so the Mac side uses the default `--host 127.0.0.1`. Only one of
  the container and a local `make sim` can run at a time (same ports).
- Colours render at their true shade; under the VM's virgl path they look lighter.

## VM (alternative)
Setup scripts and steps: `ros2/vm/README.md`.
- Apple Silicon host: Ubuntu 24.04.5 arm64 in UTM (QEMU backend, `virtio-gpu-gl-pci`), ROS2 Jazzy desktop.
- Graphics: UTM's GPU path (virgl) gives the guest OpenGL 2.1 / GLES 3.0 only, no core profile. RViz2 runs on it
  (`OpenGl version: 2.1 (GLSL 1.2)`, ~30 fps on the default scene). If a display type renders wrong, fall back to
  software rendering with `LIBGL_ALWAYS_SOFTWARE=1` (llvmpipe, OpenGL 4.5, slower).
- Network: a host-only or shared network the Mac can reach (a fixed VM address is easiest).
  The simulator binds `0.0.0.0` in the VM; ports above must be reachable from the Mac.
- The admin port (9000) is a simulator-only back door: what an operator does by hand (load or remove racks),
  fault injection, and ground truth for tests. It never tells a client where things are; positions come from the
  controller's deck file (`src/controller/decks/workcell.json`). Real hardware has no admin port, so the
  controller treats it as optional.

## RViz scene (to build)
| Piece | ROS | Source of truth |
|---|---|---|
| Arm: base, link 1, link 2, Z quill, gripper | URDF (simple primitive shapes) + `robot_state_publisher` | `/joint_states` from arm pose (already published) |
| Gripper open/closed | joint in URDF | `gripper_closed` |
| Deck, 8 rack bays, racks (present/absent), barcode labels | `MarkerArray` | world racks |
| Vials in wells, in the gripper, on the balance pan; heated vials a warmer colour | `MarkerArray` | world vials |
| Balance, scanner well, heater well; heater glow and live temperature label, scan beam while reading | `MarkerArray` | device state |
| Arm mode / collision / last error as text over the arm | text marker | arm feedback |

All dimensions come from `simulator/config.py` (`Layout`), so the scene and the physics never disagree.
One launch file starts `sim_node`, `robot_state_publisher` and RViz with a saved config; parameters:
`time_scale`, `demo_racks`, `host`.

## Code in the VM
The VM mounts this repo live from the Mac (UTM VirtFS share at `~/agentic-orchestration`) and builds it into a VM-local colcon
workspace (`~/ws`, symlinked install). The ROS package installs `simulator` from `src/` alongside itself, so
the VM needs nothing else.

## Threading
- `sim_node` reads simulator state from the ROS thread while the device servers run on an asyncio thread.
  It copies state on the simulator's thread (`_on_loop`), so reads never see a half-updated world.
