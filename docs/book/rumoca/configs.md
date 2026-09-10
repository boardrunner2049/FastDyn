# Read the complete run configurations

These are the versioned files that the configuration generator uses. Expand a vehicle to
see its firmware binary, memory layout, QEMU timing, FMU selection, and helper
commands together. `fastdyn-config` replaces tool and output locations for your
machine and materializes the legacy environment defaults as literal TOML.

These are the same base configurations used in [the first mission](getting-started.md)
and [other models](running-models.md). The outputs below use a separate directory
so you can inspect them without replacing your mission TOML. The archived
Plane recording uses an older plant; the Plane configuration here selects the
current three-wheel model, whose FMI export is still pending compiler support.

## Copter 4.6.2

```bash
fastdyn-config --base configs/copter462.toml --output out/current/copter.toml
```

<details><summary>configs/copter462.toml</summary>

```toml
{{#include ../../../configs/copter462.toml}}
```

</details>

## Plane 4.6.2

```bash
# Configuration inspection only: Plane FMI contact-event support is pending.
fastdyn-config --base configs/plane462.toml --output out/current/plane.toml
```

<details><summary>configs/plane462.toml</summary>

```toml
{{#include ../../../configs/plane462.toml}}
```

</details>

## Rover 4.6.2

```bash
fastdyn-config --base configs/rover462.toml --output out/current/rover.toml
```

<details><summary>configs/rover462.toml</summary>

```toml
{{#include ../../../configs/rover462.toml}}
```

</details>

## Plane template and landing gear

`FastDyn.Plane` uses `Vehicles.Templates.FixedWingPlant` directly. Its three
wheel contacts generate normal forces, tangential friction, and moments about
the CG. The template includes a tailwheel steering term. The wrapper converts
FLU plant signals to the FRD sensor interface expected by the firmware.

The 5.5 kg mass, 2.1 m span, inertia, and wheel locations below are explicit
tutorial assumptions. They retain the larger aircraft scale; the template's
defaults describe a much smaller aircraft. This updated plant still requires
FMI event support and subsequent flight validation and gain checks.

```modelica
{{#include ../../../modelica/FastDyn/Plane.mo}}
```
