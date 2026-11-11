# Betaflight hardware-target version sweep

The [sweep README](../virtuals/physics/flight_controllers/courbet/betaflight/version_sweep/README.md)
is the main report. It covers **2026.6.2, 2025.12.5, 4.5.5, 4.4.3, 4.3.2 and
4.2.11** with F405 / MPU6000 SPI / BMP280 I2C hardware drivers. All six completed
the declared host-piloted mission, landing and disarm on 2026-11-11. The existing
development-snapshot default remains unchanged.

The report includes measured build/rehosting/validation time, failed attempts,
exact source and board config commits, compilers, ELF hashes, ABI changes,
workarounds, sensor statistics and flight-quality limits. Per-version reports
and replayable telemetry sit beside each pinned firmware.

From the FastDyn root:

```bash
BF_SWEEP=virtuals/physics/flight_controllers/courbet/betaflight/version_sweep
fastdyn-env/bin/python "$BF_SWEEP/run.py" 2026.6.2
```

For SSH viewing, forward local port 18500 to remote port 18500 and open
`http://127.0.0.1:18500/mavcesium/` during flight. The sweep README provides
rebuild, replay and alternative-port commands.

The [hardware guide](BetaflightHardwareRehosting.md) describes the original
`2026.12.0` snapshot. The per-version reports preserve the observed failures and resolutions. The sweep uses firmware slicing of physical sensor
transports; completed missions do not establish full hardware timing or an
onboard waypoint executor.
