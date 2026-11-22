# Betaflight 4.3.2

- Target: MATEKF405 legacy board target; GCC 9.3.1.
- Source: `60c9521da6072e6c891bd30fd772e91a790f0b53`.
- Validation: physical MPU6000/BMP280 feedback, 3/3 waypoints, return, landing and disarm.
- Peak height: 17.0 m against a 15 m target.
- Version detail: Legacy motor/UART ABI; MSP receiver and linear ANGLE.

Run from the FastDyn root:

```bash
fastdyn-env/bin/python virtuals/physics/flight_controllers/courbet/betaflight/version_sweep/run.py 4.3.2
```

[Setup and metrics](../README.md) · [Profile](profile.toml)
