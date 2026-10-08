"""Tests for ROS 2 graph extraction (no ROS installation required)."""

from __future__ import annotations

import shutil

import pytest
from diagram_codebase.config import DEFAULTS
from diagram_codebase.model.builder import ModelBuilder
from diagram_codebase.scan.inventory import build_inventory
from diagram_codebase.scan.lang_cpp import analyze_cpp
from diagram_codebase.scan.lang_python import analyze_python
from diagram_codebase.scan.manifests import parse_manifests
from diagram_codebase.scan.ros2 import Ros2Extractor, norm_topic


def extract(root):
    inv = build_inventory(root, DEFAULTS)
    manifests = parse_manifests(root, inv["files"])
    b = ModelBuilder()
    idx = analyze_python(root, inv["files"], b)
    analyze_cpp(root, inv["files"], b)
    ros = Ros2Extractor(root, b)
    ros.from_python(idx)
    ros.from_cpp(inv["files"])
    ros.from_launch(inv["files"], manifests, idx)
    return b, ros


def edge(b, kind, src, dst):
    return b.edges.get(f"e:{kind}:{src}>{dst}")


@pytest.fixture
def e_ros2(fixtures_dir, tmp_path):
    dst = tmp_path / "e_ros2"
    shutil.copytree(fixtures_dir / "e_ros2", dst)
    return extract(dst)


PY = "fn:src/lidar_localization/lidar_localization/"
CPP = "fn:src/motion_controller/src/controller.cpp:Controller."


def test_norm_topic():
    assert norm_topic("scan") == "/scan"
    assert norm_topic("scan", "/robot1/") == "/robot1/scan"
    assert norm_topic("/abs", "ns") == "/abs"
    assert norm_topic("~/private") == "~/private"
    assert norm_topic("") == ""


def test_python_nodes_interfaces_and_callbacks(e_ros2):
    b, ros = e_ros2
    assert ros.found
    sf = b.nodes["ros:scan_filter"]
    assert sf["metadata"]["implementation"].endswith("scan_filter.py:ScanFilter")
    assert sf["metadata"]["language"] == "python"
    assert edge(b, "publishes", "ros:scan_filter", "topic:/scan_filtered")["payload"] == [
        "sensor_msgs/LaserScan"
    ]
    assert edge(b, "subscribes", "topic:/scan_filtered", "ros:localizer")
    assert (
        edge(b, "triggers", "topic:/scan_filtered", PY + "localizer.py:Localizer.on_scan")["label"]
        == "callback"
    )
    # publish() inside the callback is attributed to the method
    assert edge(b, "publishes", PY + "localizer.py:Localizer.on_scan", "topic:/pose")
    assert edge(b, "service_provide", "ros:localizer", "srv:/relocalize")
    assert edge(b, "triggers", "srv:/relocalize", PY + "localizer.py:Localizer.on_relocalize")
    timer = edge(b, "triggers", "ros:localizer", PY + "localizer.py:Localizer.publish_tf")
    assert timer["label"] == "timer 0.1s"


def test_parameters(e_ros2):
    b, _ = e_ros2
    p = b.nodes["param:scan_filter/max_range"]
    assert p["kind"] == "config" and p["metadata"]["default"] == 30.0
    assert b.nodes["param:localizer/map_frame"]["metadata"]["default"] == "map"
    assert edge(b, "config_dependency", "ros:controller", "param:controller/max_speed")


def test_tf_broadcast_and_lookup(e_ros2):
    b, _ = e_ros2
    assert edge(b, "publishes", "ros:localizer", "topic:/tf")
    tf = edge(b, "tf_transform", "tf:map", "tf:odom")
    assert tf["metadata"]["broadcaster"] == "ros:localizer"
    # The PoseStamped header (frame_id only, no child) does not create a TF edge.
    assert [
        e for e in b.edges.values() if e["kind"] == "tf_transform" and e["to"] == "tf:map"
    ] == []
    lookup = edge(b, "tf_transform", "tf:map", "tf:base_link")
    assert lookup["label"] == "looked up" and lookup["metadata"]["consumer"] == "ros:controller"


def test_cpp_node(e_ros2):
    b, _ = e_ros2
    c = b.nodes["ros:controller"]
    assert (
        c["metadata"]["implementation"] == "cls:src/motion_controller/src/controller.cpp:Controller"
    )
    assert edge(b, "subscribes", "topic:/pose", "ros:controller")["payload"] == [
        "geometry_msgs/PoseStamped"
    ]
    assert edge(b, "triggers", "topic:/pose", CPP + "on_pose")
    assert edge(b, "triggers", "ros:controller", CPP + "control_step")["label"] == "timer 100ms"
    assert edge(b, "service_call", "ros:controller", "srv:/relocalize")


def test_launch_remaps_rewrite_topic_ids(e_ros2):
    b, _ = e_ros2
    # Python launch: scan -> /lidar/scan for scan_filter, including its callback edge
    assert "topic:/scan" not in b.nodes
    sub = edge(b, "subscribes", "topic:/lidar/scan", "ros:scan_filter")
    assert sub["metadata"]["remapped_from"] == "topic:/scan"
    assert edge(b, "triggers", "topic:/lidar/scan", PY + "scan_filter.py:ScanFilter.on_scan")
    # XML launch: cmd_vel -> /base/cmd_vel for controller
    assert "topic:/cmd_vel" not in b.nodes
    assert edge(b, "publishes", "ros:controller", "topic:/base/cmd_vel")
    launch_py = "launch:src/lidar_localization/launch/localization.launch.py"
    assert edge(b, "launches", launch_py, "ros:scan_filter")
    assert edge(b, "launches", launch_py, "ros:localizer")
    assert edge(
        b, "launches", "launch:src/motion_controller/launch/system.launch.xml", "ros:controller"
    )


NODES_FILE = (
    "import rclpy\n"
    "from rclpy.node import Node\n"
    "from std_msgs.msg import String\n"
    "from example_interfaces.action import Fibonacci\n"
    "from rclpy.action import ActionServer, ActionClient\n"
    "\n"
    "\n"
    "class Talker(Node):\n"
    "    def __init__(self):\n"
    "        super().__init__('talker')\n"
    "        self.pub = self.create_publisher(String, 'chatter', 10)\n"
    "        self.srv = ActionServer(self, Fibonacci, 'fib', self.execute)\n"
    "\n"
    "    def execute(self, goal):\n"
    "        self.pub.publish(String())\n"
    "\n"
    "\n"
    "class Listener(Node):\n"
    "    def __init__(self):\n"
    "        super().__init__(node_name='listener')\n"
    "        self.create_subscription(String, 'chatter', self.on_msg, 10)\n"
    "        self.client = ActionClient(self, Fibonacci, 'fib')\n"
    "\n"
    "    def on_msg(self, msg):\n"
    "        pass\n"
    "\n"
    "\n"
    "def standalone():\n"
    "    node = rclpy.create_node('helper')\n"
)

LAUNCH = (
    "from launch_ros.actions import Node\n"
    "def generate_launch_description():\n"
    "    return [Node(package='demo', executable='listener', name='listener', namespace='robot1',\n"
    "                 remappings=[('chatter', 'speech')]),\n"
    "            Node(package='demo', executable='missing_exe')]\n"
)

SETUP = (
    "setup(entry_points={'console_scripts': [\n"
    "    'talker = demo.nodes:main', 'listener = demo.nodes:main']})\n"
)


def test_multi_node_file_owners_actions_and_namespaced_remap(make_repo):
    root = make_repo(
        {
            "demo/package.xml": "<package><name>demo</name><export><build_type>ament_python</build_type></export></package>",
            "demo/setup.py": SETUP,
            "demo/demo/__init__.py": "",
            "demo/demo/nodes.py": NODES_FILE,
            "demo/launch/demo.launch.py": LAUNCH,
        }
    )
    b, _ = extract(root)
    assert {n for n in b.nodes if n.startswith("ros:")} == {
        "ros:talker",
        "ros:listener",
        "ros:helper",
    }
    # Ownership follows the enclosing Node subclass, not file position.
    assert edge(b, "publishes", "ros:talker", "topic:/chatter")
    assert edge(b, "action_provide", "ros:talker", "action:/fib")
    assert edge(b, "triggers", "action:/fib", "fn:demo/demo/nodes.py:Talker.execute")
    assert edge(b, "action_call", "ros:listener", "action:/fib")
    assert b.nodes["action:/fib"]["metadata"]["interface_types"] == ["example_interfaces/Fibonacci"]
    # Listener's remap (with namespace) moves only the listener's edges.
    assert edge(b, "subscribes", "topic:/robot1/speech", "ros:listener")
    assert edge(b, "triggers", "topic:/robot1/speech", "fn:demo/demo/nodes.py:Listener.on_msg")
    assert "topic:/chatter" in b.nodes  # still published by talker
    assert not edge(b, "subscribes", "topic:/chatter", "ros:listener")
    launch = b.nodes["launch:demo/launch/demo.launch.py"]
    assert launch["metadata"]["unresolved_executables"] == ["missing_exe"]
    assert (
        edge(b, "launches", "launch:demo/launch/demo.launch.py", "ros:listener")["label"]
        == "ns robot1"
    )


def test_function_publish_edges_follow_remap(make_repo):
    root = make_repo(
        {
            "p/talk.py": (
                "import rclpy\nfrom rclpy.node import Node\nfrom std_msgs.msg import String\n\n\n"
                "class T(Node):\n    def __init__(self):\n        super().__init__('t')\n"
                "        self.pub = self.create_publisher(String, 'out', 10)\n\n"
                "    def tick(self):\n        self.pub.publish(String())\n"
            ),
            "p/launch/x.launch.xml": '<launch><node pkg="p" exec="t" name="t"><remap from="out" to="/renamed"/></node></launch>',
        }
    )
    b, _ = extract(root)
    assert "topic:/out" not in b.nodes
    assert edge(b, "publishes", "ros:t", "topic:/renamed")
    assert edge(b, "publishes", "fn:p/talk.py:T.tick", "topic:/renamed")


def test_no_ros_code_means_nothing_found(make_repo):
    b, ros = extract(
        make_repo({"a.py": "def publish(x):\n    pass\n", "b.cpp": "int main() { return 0; }\n"})
    )
    assert not ros.found
    assert not [n for n in b.nodes.values() if n["kind"] in ("ros_node", "topic")]
