#!/usr/bin/env bash
# Record the container's virtual display straight to a file (no VNC compression).
#   ros2/docker/record.sh [seconds] [name]     -> ros2/docker/recordings/<name>.mp4
set -euo pipefail
secs=${1:-30}; name=${2:-clip-$(date +%H%M%S)}
docker exec workcell-sim bash -c "ffmpeg -loglevel error -y -f x11grab -draw_mouse 0 -framerate 30 -video_size \$(echo \$SCREEN | cut -dx -f1-2) \
  -i \$DISPLAY -t $secs -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p /recordings/$name.mp4"
echo "ros2/docker/recordings/$name.mp4"
