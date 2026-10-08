# ROS 2

The scanner already extracts, without a ROS install: `ros_node`s (rclpy and
rclcpp), topics, services, actions, TF frames, declared parameters, launch files
with namespaces and remaps, and `setup.py`/CMake executables. Start from the
scanned graph in the brief; your job is the semantics it cannot see.

## Look for

- **Nodes**: `rclpy.node.Node` / `rclcpp::Node` subclasses, composable components
  (`RCLCPP_COMPONENTS_REGISTER_NODE`), lifecycle nodes (`LifecycleNode`:
  configure/activate transitions are a state machine worth extracting).
- **Topics / pub-sub**: `create_publisher`, `create_subscription`; message types;
  what the callback does with each message and what it publishes in response.
- **Services / actions**: `create_service`, `create_client` + `call_async`,
  `ActionServer` / `ActionClient`; goal → feedback → result flow.
- **Parameters**: `declare_parameter`, `get_parameter`, parameter YAML files
  passed in launch, `add_on_set_parameters_callback`.
- **Launch**: `launch.py` / `.launch.xml`: which executables become which nodes,
  `namespace`, `remappings`, `parameters`, conditions (`IfCondition`), included
  launch files, composable node containers.
- **TF**: `TransformBroadcaster` / `StaticTransformBroadcaster`, `lookup_transform`
  (consumer), URDF/xacro (`robot_state_publisher`) for the static tree.
- **Sensor pipelines**: driver → filter → fusion/estimation → planner/controller →
  actuator; the message type at each hop is the representation.
- **Executors and callback groups**: `MultiThreadedExecutor`,
  `MutuallyExclusiveCallbackGroup` / `ReentrantCallbackGroup`, timers
  (`create_timer` period). They decide what runs concurrently.
- **QoS**: reliability, durability (`TRANSIENT_LOCAL` latched topics), depth,
  sensor-data profiles; mismatched QoS silently breaks a link.

## Map to the model

| Code | Node / edge |
|---|---|
| Node | `ros_node` `ros:<name>` (name after launch remap/namespace) |
| Publisher / subscription | `topic:/<ns>/<name>`; `publishes` node→topic, `subscribes` topic→node; payload = msg type |
| Service server / client | `srv:/<name>`; `service_provide` server→srv, `service_call` client→srv |
| Action server / client | `action:/<name>`; `action_provide`, `action_call` |
| Broadcast transform parent→child | `tf:<parent>` `tf_transform` → `tf:<child>` |
| Parameter | `param:<node>/<name>` (`config`) + `config_dependency` |
| Launch file starts node | `launch:<path>` `launches` → `ros:<name>` |
| Timer | `triggers` from the timer to the callback, label with the period |
| Callback processing a message | `dataflows` stage (`transform`), representation = msg type |

## Pitfalls

- A topic name in code is relative: the final name depends on the node namespace,
  launch `namespace`, and remaps. Use the scanner's resolved names; if you
  resolve one yourself, cite the launch line.
- `~/topic` is private (node-scoped).
- Declaring a publisher is `init`; publishing in a callback is `runtime`. A
  publisher that is created but never `publish()`ed is declared, not active.
- Topics used only by `ros2 bag`, rviz configs, or tests are not production links.
- Remaps in a launch file apply only to the nodes it starts; the same executable
  started elsewhere keeps its original names.
- `use_sim_time` and conditional launch arguments change the graph; note which
  configuration you traced.
- Interfaces in `.msg/.srv/.action` files are types, not channels.
