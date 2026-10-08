# C / C++

The scanner extracts structure textually (`cpp_text`, `lang_cpp`): files,
includes, classes, functions it could recognize. Treat its call edges as
best-effort; verify any you build a flow on.

## Look for

- Entry: `int main(`, library init functions, plugin registration macros,
  RTOS/task creation (`xTaskCreate`), `std::thread` launched from `main`.
- Build: `CMakeLists.txt` (`add_executable`, `add_library`,
  `target_link_libraries`), Bazel `BUILD`, Makefiles, `package.xml` for ROS. Each
  executable/library target is a subsystem or `package` candidate.
- Concurrency: `std::thread`, `std::async`, thread pools, mutex-protected queues,
  condition variables, callbacks registered with frameworks.
- I/O: sockets, serial/CAN drivers, file I/O, shared memory, DDS/ROS.
- Termination: signal handlers, `std::atexit`, shutdown flags checked in loops,
  destructors that join threads.

## Map to the model

| Code | Node / edge |
|---|---|
| `add_executable(x ...)` | `service` or `deployment_unit` `deploy:x`; `depends_on` to linked libraries |
| `add_library(y ...)` | `package` / `library` |
| Producer/consumer queue between threads | `event_channel` `chan:<name>` + `publishes`/`subscribes`, steps of kind `async` |
| Callback registration (`setCallback(f)`) | `triggers` source → callback, phase `init` for the registration |
| Hardware/driver boundary | `external_service` (`ext:<device>`) or `datastore` for persistent media |

## Pitfalls

- `#include` is not a call and often not even a dependency on the `.cpp`.
- Header-only declarations vs definitions: cite the definition (or the call
  site), not the prototype.
- Virtual dispatch and function pointers: the target is runtime-chosen; resolve
  through the construction site (`static_inferred`) or add an uncertainty.
- Macros can generate functions and registrations; read the macro before citing.
- `#ifdef` branches: note which configuration you traced.
