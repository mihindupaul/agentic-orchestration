# Simulator + RViz in a container

The default way to run the simulated workcell with its 3D view. Needs Docker (OrbStack or Docker Desktop).

```
make sim-up        # build and start (first build pulls ROS2 Jazzy, a few minutes)
make sim-view      # http://localhost:6080/vnc.html?autoconnect=1&resize=scale
make sim-record SECS=60 NAME=take1     # clip in ros2/docker/recordings/take1.mp4
make sim-logs
make sim-down
```

- `Dockerfile`: ROS2 Jazzy, RViz2, a virtual display (Xvfb), VNC + noVNC for the browser, ffmpeg.
- `start.sh`: starts the display and the browser view, builds `ros2/workcell_sim_ros` from the mounted repo, makes
  the RViz window fill the screen, then runs `ros2 launch workcell_sim_ros sim.launch.py`. Extra launch arguments
  go in `command:` in `compose.yaml`, e.g. `time_scale:=2.0`.
- `compose.yaml`: mounts the repo read-only (edits need only a restart) and publishes the device ports
  (29999, 30003, 30004, 5001-5003, 9000) and the view (6080) on localhost.
- `record.sh`: records the virtual display directly, so clips have no VNC compression.

Rendering is software OpenGL, since containers on a Mac get no GPU; see `docs/simulator-vm.md` for numbers.
