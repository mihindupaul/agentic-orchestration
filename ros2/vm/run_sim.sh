#!/usr/bin/env bash
# In the VM: start simulator + RViz on the VM desktop. Works from a desktop terminal or over ssh.
# Extra arguments go to the launch file, e.g. time_scale:=2.0 rviz:=false
source /opt/ros/jazzy/setup.bash
source "${WORKCELL_WS:-$HOME/ws}/install/setup.bash"     # built by deploy.sh from the shared repo
export DISPLAY=${DISPLAY:-:0}
[[ -z ${XAUTHORITY:-} ]] && export XAUTHORITY=$(ls /run/user/$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1)
exec ros2 launch workcell_sim_ros sim.launch.py "$@"
