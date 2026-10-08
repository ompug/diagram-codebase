from setuptools import setup

package_name = "lidar_localization"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[("share/" + package_name + "/launch", ["launch/localization.launch.py"])],
    install_requires=["setuptools"],
    entry_points={
        "console_scripts": [
            "scan_filter = lidar_localization.scan_filter:main",
            "localizer = lidar_localization.localizer:main",
        ],
    },
)
