"""Simulator + robot_state_publisher + RViz in one go.

    ros2 launch workcell_sim_ros sim.launch.py                  # device ports open on all interfaces
    ros2 launch workcell_sim_ros sim.launch.py time_scale:=2.0 rviz:=false
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from workcell_sim_ros.scene import urdf


def generate_launch_description():
    args = [
        DeclareLaunchArgument("host", default_value="0.0.0.0", description="interface for the device ports"),
        DeclareLaunchArgument("time_scale", default_value="1.0", description=">1 runs faster than real time"),
        DeclareLaunchArgument("demo_racks", default_value="true", description="full racks in bays 3 and 2 (red caps), empty in bay 8"),
        DeclareLaunchArgument("rviz", default_value="true"),
    ]
    rviz_config = os.path.join(get_package_share_directory("workcell_sim_ros"), "rviz", "sim.rviz")
    return LaunchDescription(args + [
        Node(package="workcell_sim_ros", executable="sim_node", output="screen",
             parameters=[{"host": LaunchConfiguration("host"),
                          "time_scale": LaunchConfiguration("time_scale"),
                          "demo_racks": LaunchConfiguration("demo_racks")}]),
        Node(package="robot_state_publisher", executable="robot_state_publisher",
             parameters=[{"robot_description": urdf()}]),
        Node(package="rviz2", executable="rviz2", arguments=["-d", rviz_config],
             condition=IfCondition(LaunchConfiguration("rviz"))),
    ])
