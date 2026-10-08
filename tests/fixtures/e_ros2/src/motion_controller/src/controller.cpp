// Follows the localized pose and publishes velocity commands.
#include <chrono>
#include <memory>

#include "geometry_msgs/msg/pose_stamped.hpp"
#include "geometry_msgs/msg/twist.hpp"
#include "rclcpp/rclcpp.hpp"
#include "std_srvs/srv/trigger.hpp"
#include "tf2_ros/buffer.h"
#include "tf2_ros/transform_listener.h"

using std::placeholders::_1;
using namespace std::chrono_literals;

class Controller : public rclcpp::Node
{
public:
  Controller()
  : Node("controller"), tf_buffer_(this->get_clock()), tf_listener_(tf_buffer_)
  {
    declare_parameter("max_speed", 0.5);
    pose_sub_ = create_subscription<geometry_msgs::msg::PoseStamped>(
      "pose", 10, std::bind(&Controller::on_pose, this, _1));
    cmd_pub_ = create_publisher<geometry_msgs::msg::Twist>("cmd_vel", 10);
    timer_ = create_wall_timer(100ms, std::bind(&Controller::control_step, this));
    relocalize_ = create_client<std_srvs::srv::Trigger>("relocalize");
  }

private:
  void on_pose(const geometry_msgs::msg::PoseStamped::SharedPtr msg)
  {
    last_pose_ = *msg;
  }

  void control_step()
  {
    auto tf = tf_buffer_.lookupTransform("base_link", "map", tf2::TimePointZero);
    geometry_msgs::msg::Twist cmd;
    cmd.linear.x = clamp_speed(0.3);
    cmd_pub_->publish(cmd);
  }

  double clamp_speed(double v)
  {
    return v > 0.5 ? 0.5 : v;
  }

  tf2_ros::Buffer tf_buffer_;
  tf2_ros::TransformListener tf_listener_;
  geometry_msgs::msg::PoseStamped last_pose_;
  rclcpp::Subscription<geometry_msgs::msg::PoseStamped>::SharedPtr pose_sub_;
  rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr cmd_pub_;
  rclcpp::TimerBase::SharedPtr timer_;
  rclcpp::Client<std_srvs::srv::Trigger>::SharedPtr relocalize_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<Controller>());
  rclcpp::shutdown();
  return 0;
}
