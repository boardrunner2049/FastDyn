# Betaflight 2025.12.5

- Target: MATEKF405CTR; GCC 13.3.1.
- Source: `7348054f268f0058574719c134e9f149565bb8ea`.
- Validation: physical MPU6000/BMP280 feedback, 3/3 waypoints, return, landing and disarm.
- Peak height: 15.0 m against a 15 m target.
- Version detail: ANGLE heartbeat mode is 0; requires GCC 13.3.1.

Run from the FastDyn root:

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/betaflight/version_sweep/run.py 2025.12.5
```

[Setup and metrics](../README.md) · [Profile](profile.toml)
