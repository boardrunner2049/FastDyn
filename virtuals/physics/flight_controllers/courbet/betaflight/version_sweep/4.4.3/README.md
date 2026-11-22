# Betaflight 4.4.3

- Target: STM32F405 + MTKS-MATEKF405CTR unified configuration; GCC 10.3.1.
- Source: `738127e7e11da587f63b9531ec732a37bdb436b0`.
- Validation: physical MPU6000/BMP280 feedback, 3/3 waypoints, return, landing and disarm.
- Peak height: 15.5 m against a 15 m target.
- Version detail: Unified board settings; linear ANGLE; flash driver compiled at -O0.

Run from the FastDyn root:

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/betaflight/version_sweep/run.py 4.4.3
```

[Setup and metrics](../README.md) · [Profile](profile.toml)
