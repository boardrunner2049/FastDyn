# Fuzzing

FastDyn has two complementary fuzzing workflows:

- The in-process LibAFL/AFLNet fuzzer targets firmware loops and input points
  directly with `coverage = true` plus `anchor` / `assert` virtual
  instructions. Docker is used there as a reproducible build environment for
  LibAFL/AFLNet dependencies; see
  `virtuals/fuzzer/fastdyn_fuzz_lib/README.md`.
- OptiFuzz / CP-Explore lives in `virtuals/fuzzer/libafl_phi`. Its default
  backend is now the high-fidelity FMUv3 ArduCopter path:

  ```bash
  source ./setup.sh --build-qemu --with-rumoca
  cd virtuals/fuzzer/libafl_phi
  cargo run --bin baby_fuzzer
  ```

  `setup.sh` creates the expected sibling Banquo parser checkout at `../banquo`
  unless `--skip-optifuzz` is used. OptiFuzz then launches
  `fastdyn run -c configs/copter462.toml`, writes each execution under
  `out/optifuzz/execution-<n>`, passes parameter mutations to the mission helper
  through `FASTDYN_OPTIFUZZ_MUTATION_BIN`, and can optionally use `fastdyn swarm`
  by setting `FASTDYN_OPTIFUZZ_SWARM_INSTANCES=2` or higher. Select another
  vehicle/config with `FASTDYN_OPTIFUZZ_VEHICLE=rover` and
  `FASTDYN_OPTIFUZZ_CONFIG=configs/rover462.toml`, or `plane` /
  `configs/plane462.toml`. Set
  `FASTDYN_OPTIFUZZ_BACKEND=gazebo` to use the older Gazebo/Courbet runner.
  To check the wiring without launching QEMU, run:

  ```bash
  FASTDYN_OPTIFUZZ_SMOKE=1 FASTDYN_OPTIFUZZ_DRY_RUN=1 cargo run --bin baby_fuzzer
  ```

  Set `FASTDYN_OPTIFUZZ_COVERAGE=1` when using a FastDyn plugin built with the
  LibAFL coverage writer; the default high-fidelity plugin leaves this off.

- `fastdyn swarm` is also available directly as a high-fidelity campaign runner
  for many full ArduCopter/FMUv3 simulations. It keeps the actual QEMU firmware,
  board tick, FMU plant, MAVProxy, MAVCesium, and mission script path intact
  while scaling out to many isolated workers on the same host.
