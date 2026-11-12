# Betaflight 4.5.5

- Target: MATEKF405CTR; GCC 10.3.1.
- Source: `4adbd3ef7cb546947600e5f747bd5453c9573063`.
- Validation: physical MPU6000/BMP280 feedback, 3/3 waypoints, return, landing and disarm.
- Peak height: 16.2 m against a 15 m target.
- Version detail: MSP receiver; velocity derived from timestamped GPS telemetry.

Run from the FastDyn root:

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/betaflight/version_sweep/run.py 4.5.5
```

[Setup and metrics](../README.md) · [Profile](profile.toml)
