# Reproducibility bundle

This directory is the portable input bundle for the FastDyn coverage runs.
It holds the exact target models, AFLNet corpora, TOML templates, and entry
points used for the four reproduced targets.  Firmware images and the
documented Modbus host bridge remain in their BoardRunner example directories.

The launchers compile models from `models/` directly into their new run
directory using `scripts/build_model.sh`.  Therefore no target-specific model
entry is required in `boardrunner_sdk/CMakeLists.txt`.

Contents:

- `models/<target>/fuzzing_model.c`: AFLNet model source for lwIP, MQTT, and
  Modbus.
- `models/<target>/base_model.c`: non-fuzzing Elder source for lwIP, MQTT,
  Modbus, and SD-card.
- `configs/aflnet`: the three AFLNet TOMLs and MQTT snapshot-range input.
- `configs/board`: hardware/Elder and minimal-QEMU TOML templates.
- `seeds`: saved fuzzing corpora.

Run directly from this directory (or use the compatibility launchers one level
above it):

```sh
./scripts/run_aflnet_coverage.sh lwip   /absolute/results/lwip
./scripts/run_aflnet_coverage.sh mqtt   /absolute/results/mqtt
./scripts/run_aflnet_coverage.sh modbus /absolute/results/modbus

./scripts/run_board_coverage.sh mqtt
./scripts/run_board_coverage.sh lwip
./scripts/run_board_coverage.sh sdcard
./scripts/run_board_coverage.sh modbus

./scripts/run_qemu_coverage.sh all
```

`scripts/build_model.sh` directly compiles and links each model against the
already-built BoardRunner VIO and FastDyn shared libraries.  No target model
must be added to, or built through, `boardrunner_sdk/CMakeLists.txt`.  The
normal runs require `boardrunner_sdk/build` and `FastDyn/build`; AFLNet runs
require their `build-aflnet` counterparts, as well as the selected `afl-fuzz`
and patched `qemu-system-arm` binaries.

The AFLNet TOMLs intentionally contain the `__MODEL_PATH__` placeholder.  The
launcher replaces it with that run's newly linked shared object, so a bundle
can be copied or run without stale paths into a CMake build tree.
