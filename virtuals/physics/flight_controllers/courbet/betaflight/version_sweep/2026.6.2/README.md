# Betaflight 2026.6.2

- Target: MATEKF405CTR; GCC 13.3.1.
- Source: `e0b7bb01b17b21351057e9ead2d1ab39dd44fa16`.
- Validation: physical MPU6000/BMP280 feedback, 3/3 waypoints, return, landing and disarm.
- Peak height: 15.1 m against a 15 m target.
- Version detail: Serial function-mask configuration; GENERIC heartbeat.

Run from the FastDyn root:

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/betaflight/version_sweep/run.py 2026.6.2
```

[Setup and metrics](../README.md) · [Profile](profile.toml)
