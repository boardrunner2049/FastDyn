# Betaflight 4.2.11

- Target: MATEKF405 legacy board target; GCC 9.2.1.
- Source: `948ba6339766851806d7637370829ea0ff74c690`.
- Validation: physical MPU6000/BMP280 feedback, 3/3 waypoints, return, landing and disarm.
- Peak height: 16.9 m against a 15 m target.
- Version detail: Legacy SPI/I2C ABI; UBX SOL/POSLLH/VELNED GPS packets.

Run from the FastDyn root:

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/betaflight/version_sweep/run.py 4.2.11
```

[Setup and metrics](../README.md) · [Profile](profile.toml)
