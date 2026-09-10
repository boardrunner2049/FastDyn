# A one-hour walkthrough

In this walkthrough, you will run a firmware-driven simulation, resize a
quadrotor, choose controller gains from measured responses, and change a
Modelica force equation. By the end, you will have your own model variant and
know how to compile it, run it in FastDyn, and compare the result with a baseline.

## Before you start

Set up [Nix, Docker, or a manual installation](../general/environment.md) and
check that the common commands work. Allow separate time for setup: the first
compiler and QEMU builds can take longer than the walkthrough itself. Keep a
terminal at the repository root and this book open beside it.

You do not need to run a tuning or Monte Carlo batch in advance. The book
includes measured gain comparisons and all 18 payload-study trajectories,
ready to explore. You can reproduce those longer experiments afterward.

## Your path through the tutorial

Allow about an hour after setup, with roughly 15 minutes for each block.
Take more time wherever you want to inspect the code or try another change.

| Approx. time | Walkthrough | Checkpoint |
| --- | --- | --- |
| 0–15 min | [Understand the workflow](workflow.md) and [FMI interface](fmi.md), then [fly your first mission](getting-started.md) | Find the firmware/plant boundary and save a baseline trajectory |
| 15–30 min | [Read the Modelica model](modelica.md) and [resize to a QAV-R](qavr.md) | Locate the geometry, inertia, motor, and sensor equations; select the smaller model |
| 30–45 min | [Compare the recorded gains](tuning.md) and [apply a load at a motor](payload.md) | Use the selected gains, edit the force equation, rebuild, and fly |
| 45–60 min | [Explore the payload study](monte-carlo.md) and [mission reports](mission-reports.md) | Compare successful and failed trajectories and identify the model's limits |

## Finish with your own physics change

The [varying-force exercise](payload.md#5-change-an-equation-rebuild-and-fly)
walks you through copying a model, changing its equations, checking the compiled
force, and flying a mission. Save your source, run configuration, and telemetry
so you can explain both what changed and how it affected the vehicle.

For a next experiment, change the load's magnitude or period, reproduce the
tuning comparison, or run a new payload batch. The [model library and
roadmap](library-roadmap.md) explains how to choose models and when a Modelica
controller simulation can complement firmware runs in FastDyn.

The hands-on exercises use Copter and QAV-R with the pinned compiler. You can
also [run Rover](running-models.md). Plane is available as source and a
historical recording while its landing-gear contact events await compiler
support. The gains in this walkthrough are tested simulation settings, not
flight-tested hardware settings.
