<section class="fd-hero">
<p class="fd-eyebrow">FastDyn · Firmware meets physics</p>
<h1>Real firmware.<br/>Your vehicle model.</h1>
<p>Connect embedded firmware to a Modelica plant. Change the physics, tune the controller, and follow the results from sensor to trajectory.</p>
</section>

<div class="fd-paths">
<a class="fd-path" href="rumoca/session.html"><strong>The Rumoca walkthrough</strong><span>Build a vehicle, fly a mission, and investigate an off-center payload.</span><b>One hour · Start the walkthrough →</b></a>
<a class="fd-path" href="general/overview.html"><strong>The FastDyn field guide</strong><span>Understand the runtime, configure a board, and work with devices and virtuals.</span><b>Reference · Explore FastDyn →</b></a>
</div>

FastDyn runs embedded firmware in a configurable QEMU environment. A TOML file
describes the CPU, memory map, firmware, peripherals, instrumentation, and any
external physics model. You can inspect firmware behavior, supply virtual
devices, connect selected hardware, and automate repeatable experiments.

## Follow the signals

```mermaid
flowchart LR
    config["TOML<br/>Configuration"] --> firmware["Firmware<br/>in QEMU"]
    firmware <-->|"Device access"| drivers["FastDyn<br/>Drivers"]
    drivers <-->|"PWM · Sensors"| plant["Modelica<br/>Physics model"]
    firmware <-->|"MAVLink"| tools["Mission commands<br/>Logs and plots"]
```

The Rumoca examples preserve the firmware's sensor and actuator driver path.
Fidelity comes from the emulated firmware, peripheral behavior, timing, and
plant working together. A detailed firmware simulation still needs a suitable,
validated physical model; an idealized plant does not reproduce every effect
of a real airframe.

To preview this book, run `mdbook serve docs --open` from your chosen development
environment and open **http://localhost:3000**. For a lightweight docs-only setup,
see [preview and publish the book](documentation.md).
