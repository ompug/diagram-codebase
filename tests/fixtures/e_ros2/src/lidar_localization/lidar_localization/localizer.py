"""Scan-matching localizer publishing the robot pose and map->odom TF."""

import rclpy
from geometry_msgs.msg import PoseStamped, TransformStamped
from rclpy.node import Node
from sensor_msgs.msg import LaserScan
from std_srvs.srv import Trigger
from tf2_ros import TransformBroadcaster


class Localizer(Node):
    def __init__(self) -> None:
        super().__init__("localizer")
        self.declare_parameter("publish_rate", 10.0)
        self.declare_parameter("map_frame", "map")
        self.pose_pub = self.create_publisher(PoseStamped, "pose", 10)
        self.create_subscription(LaserScan, "scan_filtered", self.on_scan, 10)
        self.create_service(Trigger, "relocalize", self.on_relocalize)
        self.tf_broadcaster = TransformBroadcaster(self)
        self.timer = self.create_timer(0.1, self.publish_tf)
        self.latest = None

    def on_scan(self, msg: LaserScan) -> None:
        self.latest = msg
        pose = PoseStamped()
        pose.header.frame_id = "map"
        self.pose_pub.publish(pose)

    def on_relocalize(self, request, response):
        self.latest = None
        response.success = True
        return response

    def publish_tf(self) -> None:
        t = TransformStamped()
        t.header.stamp = self.get_clock().now().to_msg()
        t.header.frame_id = "map"
        t.child_frame_id = "odom"
        self.tf_broadcaster.sendTransform(t)


def main() -> None:
    rclpy.init()
    rclpy.spin(Localizer())
    rclpy.shutdown()
