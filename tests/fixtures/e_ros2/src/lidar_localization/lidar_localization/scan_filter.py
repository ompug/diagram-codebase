"""Drops out-of-range returns from the raw lidar scan."""

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan


class ScanFilter(Node):
    def __init__(self) -> None:
        super().__init__("scan_filter")
        self.declare_parameter("max_range", 30.0)
        self.max_range = self.get_parameter("max_range").value
        self.pub = self.create_publisher(LaserScan, "scan_filtered", 10)
        self.sub = self.create_subscription(LaserScan, "scan", self.on_scan, 10)

    def on_scan(self, msg: LaserScan) -> None:
        msg.ranges = [r if r < self.max_range else float("inf") for r in msg.ranges]
        self.pub.publish(msg)


def main() -> None:
    rclpy.init()
    rclpy.spin(ScanFilter())
    rclpy.shutdown()
