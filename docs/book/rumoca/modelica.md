# Read the Modelica model

## Know which source your run uses

The first mission uses `modelica/FastDyn/Copter.mo`, shown below, with the
library in `third_party/common/modelica_models`. The QAV-R models extend the
same wrapper. Check the active entry's `model_file`, `model`, `source_roots`,
`output`, and `[FMU].compiler` in your generated TOML before an experiment.

Keep edited models in your source directory. Study scripts regenerate their
files under `out/`; edits to those generated copies will not persist.

## A little Modelica before the full model

| Construct | Meaning in this vehicle |
| --- | --- |
| `within FastDyn;` | Put the class in the `FastDyn` package |
| `model Qavr ... end Qavr;` | Define the named model `FastDyn.Qavr` |
| `parameter Real mass = 0.5;` | A real-valued quantity fixed during this experiment, in kg by convention |
| `Real force_b[3];` | A vector with three real components; Modelica indices start at 1 |
| `Real inertia[3,3];` | A 3-by-3 matrix; `{...}` constructs a vector and `[...]` can construct a matrix |
| `extends Copter(mass = bare_mass);` | Reuse the parent model and change a parameter binding |
| `equation` | Introduce relationships the compiler must satisfy, rather than a sequence of assignments |
| `der(omega)` | The time derivative of motor speed; this introduces dynamics |

Changing `tau_up` adjusts the motor law already present. Replacing a motor law
or adding an external-force equation changes the physics represented by the
model. In both cases, keep the exported actuator and sensor contract intact
unless you also intend to change FastDyn's physics backend.

## 1. Open the vehicle wrapper

The source below is included directly from `modelica/FastDyn/Copter.mo`, so it
matches the file in this checkout. It connects a reusable plant to the PWM and
sensor interface expected by the ArduPilot drivers.

<details><summary>Show the complete FastDyn.Copter Modelica model</summary>

```modelica
{{#include ../../../modelica/FastDyn/Copter.mo}}
```

</details>

## 2. Follow the physics

The wrapper's `plant` adapts the library's
[`Vehicles.Templates.QuadrotorPlant`](https://github.com/CogniPilot/modelica_models/blob/dfdb3294f61ab639a8a8be19611a1f69187a3ff7/Vehicles/Templates/QuadrotorPlant.mo).
The local `QuadrotorWithExternalWrench` adds external force and moment inputs
for the [load exercise](payload.md); those inputs default to zero.
Its actuator inputs are four motor speeds. Four motor states lag their commands,
and each motor produces thrust proportional to the square of its speed. The motor
moment map turns those thrusts into roll, pitch, and yaw moments. Body drag and
ground contacts add forces and moments before rigid-body integration.

```modelica
// The motor is a dynamic state, not an instantaneous actuator.
der(omega) = tau_inv * omega_error;
thrust = Ct * omega * omega;

// Four thrusts produce a body moment through the motor geometry.
M_rotor = motor_moment_map * F_m;
```

Find these equations in the library file. Change `tau_up` and `tau_down` to
model a different motor response; change the equations to model another
actuator law. A new law should be checked against measured actuator data.

## 3. Keep arrays as arrays

The inertia tensor is a `Real[3,3]`; drag areas, forces, and sensor vectors are
`Real[3]`. The wrapper supplies `J = inertia` to the rigid body. Keeping this
as a matrix makes the Modelica equations match the physical notation.

The QAV-R uses its estimated inertia tensor. The payload experiment
keeps the vehicle mass and inertia fixed and adds an external force
and its moment instead, so these are two separate modeling experiments.

## 4. Inspect the compiler output

In your chosen environment, run:

```bash
rumoca compile modelica/FastDyn/Qavr.mo --model FastDyn.Qavr \
  --source-root modelica --source-root third_party/common/modelica_models \
  --emit dae-json --output out/qavr-dae.json
```

Expected output includes:

```text
wrote Dae IR (json) to out/qavr-dae.json
```

This checks name resolution and lowering. It does not by itself establish that
the FMI exporter supports every feature in the lowered model, or that the
resulting plant flies correctly. Those are separate build and simulation
checkpoints.

## 5. Check the frame convention

The plant's body axes are **forward, left, up**. The firmware interface uses
**forward, right, down**, so the wrapper changes the signs of Y and Z sensor
components. This tutorial chooses world X=north, Y=west, Z=up and converts GPS
velocity to NED. Its magnetic-field vector is configured consistently with
that choice; a frame label alone cannot determine the numerical rotation.

## Diagnose one stage at a time

| Checkpoint | Evidence to look for | If it fails |
| --- | --- | --- |
| Source selection | The generated TOML points to your edited file and intended class | Correct the active model, source roots, or overlay |
| Modelica compilation | The DAE command above finishes | Read the parser, name-resolution, or equation diagnostic |
| FMI export and native build | An FMU and its host library are produced | Diagnose exporter support or the C build before trying controller changes |
| Initialization | Valid parameters, finite stationary sensors, correct gravity and actuator directions | Check units, axes, initial conditions, and parameter bindings |
| Mission | Readiness, arming, waypoint progress, and the required final condition | Use the console and telemetry to distinguish startup errors from flight behavior |
| Comparison | Baseline and changed-model logs, with the same firmware and gains | Verify that a rebuilt FMU was loaded and that no unrelated settings changed |

When moving between compiler generations, choose a fresh FMU `output` directory
or move your old generated output aside. Rumoca can refuse to overwrite an
archive from an older format (`not a recognized previous product`); that is an
artifact conflict, not a failure of your new Modelica equations.

A changed source file or a successful DAE export alone is not the final result.
For your own physics experiment, retain the source, run TOML, compiler revision,
FMU, and telemetry together so you or someone else can reproduce the comparison.

Next, [select the QAV-R model](qavr.md) and see which physical properties change
when you move to a smaller airframe.
