from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription(
        [
            Node(
                package="lidar_localization",
                executable="scan_filter",
                name="scan_filter",
                remappings=[("scan", "/lidar/scan")],
                parameters=[{"max_range": 25.0}],
            ),
            Node(
                package="lidar_localization",
                executable="localizer",
                name="localizer",
            ),
        ]
    )
