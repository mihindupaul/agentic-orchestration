# Simulator + RViz in a container, shown in the browser (noVNC) and recordable from the virtual display.
# Rendering is software OpenGL (Mesa llvmpipe): containers on a Mac have no GPU.
FROM ros:jazzy-ros-base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-jazzy-rviz2 ros-jazzy-robot-state-publisher python3-colcon-common-extensions \
        xvfb x11-utils x11vnc novnc python3-websockify xdotool ffmpeg \
        libgl1-mesa-dri mesa-utils fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV DISPLAY=:99 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    SCREEN=1920x1080x24 \
    WORKCELL_WS=/ws

COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

# device ports, then the browser view
EXPOSE 29999 30003 30004 5001 5002 5003 9000 6080
ENTRYPOINT ["/usr/local/bin/start.sh"]
