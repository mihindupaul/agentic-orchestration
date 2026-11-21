"""ROS2 node that hosts the simulator and shows it in RViz.

Publishes
  /joint_states             sensor_msgs/JointState            arm + gripper joints (drives robot_state_publisher)
  /workcell/scene_static   visualization_msgs/MarkerArray    deck, slots, stations (latched)
  /workcell/scene          visualization_msgs/MarkerArray    racks, vials, activity, status (changes only)
  /workcell/state          std_msgs/String                   JSON snapshot of arm, world and faults
  /workcell/events         std_msgs/String                   one JSON event per message

The device TCP servers run in the same process, so an orchestrator on another machine drives the
simulator over the network while RViz shows it here.
"""

import asyncio
import json
import math
import threading

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Point
from rclpy.qos import DurabilityPolicy, QoSProfile
from sensor_msgs.msg import JointState
from std_msgs.msg import String
from visualization_msgs.msg import Marker, MarkerArray

from workcell_sim import Simulator, Timing
from workcell_sim.simulator import load_demo_racks

from . import scene

MARKER_TYPES = {"cube": Marker.CUBE, "sphere": Marker.SPHERE, "cylinder": Marker.CYLINDER,
                "text": Marker.TEXT_VIEW_FACING, "triangles": Marker.TRIANGLE_LIST}
FULL_REFRESH_S = 5.0     # resend every marker now and then so a late RViz catches up


class SimNode(Node):
    def __init__(self):
        super().__init__("workcell_sim")
        self.declare_parameter("host", "0.0.0.0")
        self.declare_parameter("time_scale", 1.0)
        self.declare_parameter("joint_hz", 30.0)
        self.declare_parameter("scene_hz", 15.0)
        self.declare_parameter("demo_racks", True)

        self.sim = Simulator(timing=Timing(time_scale=float(self.get_parameter("time_scale").value)))
        if self.get_parameter("demo_racks").value:
            load_demo_racks(self.sim.world)

        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.js_pub = self.create_publisher(JointState, "joint_states", 10)
        self.static_pub = self.create_publisher(MarkerArray, "workcell/scene_static", latched)
        self.scene_pub = self.create_publisher(MarkerArray, "workcell/scene", 10)
        self.state_pub = self.create_publisher(String, "workcell/state", 10)
        self.event_pub = self.create_publisher(String, "workcell/events", 50)
        self._events_sent = 0
        self._shown: dict = {}
        self._since_full = FULL_REFRESH_S

        self._loop = asyncio.new_event_loop()
        self._boot_error = None
        ready = threading.Event()
        threading.Thread(target=self._serve, args=(ready,), daemon=True).start()
        if not ready.wait(timeout=5.0) or self._boot_error:
            # Usually another simulator still holds the ports; publishing a frozen arm next to it makes RViz flicker.
            raise RuntimeError(f"simulator did not start: {self._boot_error or 'timeout'}")

        self.static_pub.publish(self._array(scene.static_markers(self.sim.layout)))
        self.create_timer(1.0 / float(self.get_parameter("joint_hz").value), self._publish_joints)
        self._scene_period = 1.0 / float(self.get_parameter("scene_hz").value)
        self.create_timer(self._scene_period, self._publish_scene)

    def _serve(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)

        async def boot():
            try:
                ports = await self.sim.start(self.get_parameter("host").value)
                self.get_logger().info(f"device ports: {ports}")
            except OSError as exc:
                self._boot_error = exc
            ready.set()

        self._loop.create_task(boot())
        self._loop.run_forever()

    def _on_loop(self, fn):
        """Run `fn` on the simulator's thread so state is never read mid-update."""
        async def call():
            return fn()
        return asyncio.run_coroutine_threadsafe(call(), self._loop).result(timeout=1.0)

    # ------------------------------------------------------------- publishing
    def _publish_joints(self) -> None:
        snap = self._on_loop(lambda: scene.snapshot(self.sim))
        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = scene.JOINTS
        msg.position = scene.joint_positions(snap)
        self.js_pub.publish(msg)

    def _publish_scene(self) -> None:
        snap, state, events = self._on_loop(lambda: (
            scene.snapshot(self.sim), json.dumps(self.sim.state()), self.sim.world.events[self._events_sent:]))
        markers = scene.dynamic_markers(snap, self.sim.layout)
        self._since_full += self._scene_period
        if self._since_full >= FULL_REFRESH_S:
            self._since_full = 0.0
            self._shown = {}
        changed, deleted, self._shown = scene.diff(self._shown, markers)
        if changed or deleted:
            arr = self._array(changed)
            for ns, mid in deleted:
                arr.markers.append(Marker(ns=ns, id=mid, action=Marker.DELETE))
            self.scene_pub.publish(arr)

        self.state_pub.publish(String(data=state))
        for ev in events:
            self.event_pub.publish(String(data=json.dumps(ev)))
        self._events_sent += len(events)

    def _array(self, markers: list) -> MarkerArray:
        stamp = self.get_clock().now().to_msg()
        arr = MarkerArray()
        for d in markers:
            m = Marker()
            m.header.frame_id = d["frame"]
            m.header.stamp = stamp
            m.frame_locked = d["frame"] != "world"
            m.ns, m.id, m.type, m.action = d["ns"], d["id"], MARKER_TYPES[d["type"]], Marker.ADD
            m.pose.position.x, m.pose.position.y, m.pose.position.z = d["xyz"]
            m.pose.orientation.z = math.sin(d["yaw"] / 2)
            m.pose.orientation.w = math.cos(d["yaw"] / 2)
            m.scale.x, m.scale.y, m.scale.z = d["scale"]
            if d["type"] == "text":
                m.scale.x = m.scale.y = m.scale.z     # RViz2 spaces words badly when x/y are 0
                m.text = d["text"]
            m.points = [Point(x=x, y=y, z=z) for x, y, z in d["points"]]
            m.color.r, m.color.g, m.color.b, m.color.a = d["rgba"]
            arr.markers.append(m)
        return arr


def main() -> None:
    rclpy.init()
    node = SimNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.try_shutdown()


if __name__ == "__main__":
    main()
