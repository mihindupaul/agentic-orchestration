#!/usr/bin/env bash
# Container entry point: virtual display, VNC + noVNC, build the ROS package from the mounted repo, launch.
# Extra arguments go to the launch file, e.g. time_scale:=2.0
set -eo pipefail      # not -u: the ROS setup scripts read unset variables
source /opt/ros/jazzy/setup.bash

Xvfb "$DISPLAY" -screen 0 "$SCREEN" +extension GLX +render -noreset &
for _ in $(seq 50); do xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break; sleep 0.1; done
x11vnc -display "$DISPLAY" -forever -shared -nopw -quiet -rfbport 5900 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

# Build from the live repo (mounted read-only at /repo); build output stays in the container.
mkdir -p "$WORKCELL_WS" && cd "$WORKCELL_WS"
colcon build --base-paths /repo/ros2 --symlink-install --event-handlers console_direct- >/dev/null
source "$WORKCELL_WS/install/setup.bash"

# Fill the virtual screen with the RViz window once it appears (no window manager, so no borders).
W=$(echo "$SCREEN" | cut -dx -f1); H=$(echo "$SCREEN" | cut -dx -f2)
( win=$(xdotool search --sync --onlyvisible --name RViz | head -1) \
  && xdotool windowmove "$win" 0 0 windowsize "$win" "$W" "$H" ) >/dev/null 2>&1 &

echo "workcell sim: device ports open, view at http://localhost:6080/vnc.html?autoconnect=1&resize=scale"
exec ros2 launch workcell_sim_ros sim.launch.py "$@"
