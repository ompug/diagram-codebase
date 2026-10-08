cmake_minimum_required(VERSION 3.8)
project(motion_controller)

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(tf2_ros REQUIRED)

add_executable(controller src/controller.cpp)
ament_target_dependencies(controller rclcpp geometry_msgs std_srvs tf2_ros)

install(TARGETS controller DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY launch DESTINATION share/${PROJECT_NAME})
ament_package()
