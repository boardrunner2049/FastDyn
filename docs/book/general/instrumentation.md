# Instrumentation

FastDyn can count function calls, watch variables, and inspect RTOS structures.
Select an instrument for the question you want to answer, configure it in the
target TOML, and keep its output with the run.

| Instrument | Use it to inspect | Start here |
| --- | --- | --- |
| Function counters | Which functions execute and how often | [Configuration and example](function-counters.md) |
| Variable watches | Values in firmware memory as execution proceeds | [Runtime behavior and fixtures](variable-watches.md) |
| RTOS introspection | Tasks, queues, and scheduler state | [FreeRTOS example](rtos.md) |

The following pages include runnable configurations and explain the extension
points used by the native plugin and Python preprocessor.
