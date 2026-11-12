# Betaflight MATEKF405CTR

Hardware-sensor baseline: Betaflight 2026.12.0 development commit `5ce286c96`,
STM32F405 with MPU6000 SPI and BMP280 I2C. The ELF is unchanged upstream firmware;
its exact hash, vectors and consumed layouts are checked by `profile.toml`.

From the FastDyn root:

```bash
fastdyn-env/bin/fastdyn run -c configs/betaflight_f405.toml
```

The host RC pilot flies three 30 m corners at 15 m, returns, lands and disarms.
The offline viewer uses port 15500 and stays available for 120 s after landing.
Set `FASTDYN_BF_VIEW_SECONDS=0` to exit immediately.

[Hardware/build details](../../../../../../docs/BetaflightHardwareRehosting.md) ·
[Release sweep](../version_sweep/README.md)
