# ArduRover Rehosting Setup

This guide covers the legacy ArduRover/Courbet Gazebo setup. The maintained default FastDyn path uses the FMU/Rumoca backend; use this flow when you specifically need the older Gazebo backend with `LIBGZ=true` and `LIBHW=true`.

## Docker Build

The easiest way to use the legacy ArduRover/Courbet backend is via Docker. A `Dockerfile` is provided that automatically handles all QEMU, Gazebo Harmonic, Rust, and `libhw` dependencies.

To build the image, run this from your **parent workspace directory** (the folder containing `FastDyn/`, `qemu/`, etc.):

```bash
cd ..
docker build -f FastDyn/Dockerfile -t fastdyn-env .
```

Then run the container interactively:

```bash
docker run -it fastdyn-env
```

You will drop into a shell inside `/workspace/FastDyn` with the environment ready. **Note: All phases below (Static Analysis, Probe Run, Trace Analyze, etc.) should be run inside this container!**

## Local Build Instructions

### Prerequisites

The trace analysis pipeline requires `universal-ctags` or `exuberant-ctags` for fallback source extraction. Please install it using your package manager (e.g., `sudo apt-get install universal-ctags`).

Gazebo Harmonic is required. If the Gazebo pkg-config modules (`gz-transport13`, `gz-msgs10`, `protobuf`) are not available, install them via:

```bash
bash virtuals/physics/flight_controllers/courbet/utils/install_gazebo_harmonic.sh
```

`LIBHW=true` also requires a buildable `libhw` checkout and `libstlink-dev`.

### Local Build

To initialize submodules, build Gazebo dependencies, and build FastDyn, run from the FastDyn root:

```bash
source ./setup.sh --build-qemu --build-gazebo --skip-optifuzz
```

_(Omit `--build-qemu` if the patched QEMU is already built)._

Then, build the `libhw` repo, by first running the setup file in libhw folder

```bash
cd ../libhw
make
```

Then, to build FastDyn correctly for this legacy backend, run:

```bash
cd ../Fastdyn
make PROBE=true DEV=true LIBHW=true LIBGZ=true FLIGHT_CONTROLLERS=true DEBUG_PRINT=true LIBFUZZ=false
```

## 1. Static Analysis Phase

Before running certain analyses or traces, execute the static analysis frontend to extract binary metadata, SVD structures, source maps, and callgraphs.

Run the static analyzer using your configuration (make sure to update the paths in the `configs/automatic_ardurover462.toml`):

```bash
fastdyn static-analyze -c configs/automatic_ardurover462.toml -s third_party/common/cmsis-svd-data --force
```

This extracts the analysis artifacts into the cache directory specified in your TOML configuration under `[Rehosting.directories]`.

## 2. Probe Run Phase

This phase runs the binary on QEMU using the FastDyn plugin and consumes artifacts from the static analysis phase (e.g., `probe_faults.json`).

Run the command using your configuration:

```bash
fastdyn probe-run -c configs/automatic_ardurover462.toml -o fastdyn_recent_run
```

This will generate the artifacts under the `fastdyn_recent_run`

## 3. Trace Analyze Phase

After a probe run completes (or hangs), the trace analyzer parses the execution artifacts (`io.log`, `BBL_counts`) and correlates them with the static analysis data to produce a structured, deterministic LLM prompt. It avoids running QEMU or directly calling the LLM.

Run the command:

```bash
fastdyn trace-analyze -c configs/automatic_ardurover462.toml -o fastdyn_work --latest-run-dir fastdyn_recent_run
```

This will generate `fastdyn_work/prompt.txt` containing the hardware configuration, stateful analysis, tight loops, and full source context, ready to be passed to an LLM.

If an LLM later writes `fastdyn_work/routing.json` and recommends creating new model files or adding TOML routing entries, rerun trace analysis with:

```bash
fastdyn trace-analyze -c configs/automatic_ardurover462.toml -o fastdyn_work --latest-run-dir fastdyn_recent_run --apply-routing
```

That mode asks before applying each file/config change, then generates the routed implementation prompt.

## 4. LLM Prompt Phase

After `trace-analyze` creates `fastdyn_work/prompt.txt`, send that prompt to the configured LLM provider:

```bash
fastdyn llm -d fastdyn_work --compile --model gpt-5.4 --reasoning-effort medium --evaluate
```

The `llm` command reads `fastdyn_work/prompt.txt`, saves the prompt and response under `fastdyn_llm_history/`, and auto-detects the target model file from `fastdyn_work/analysis.json` when `-o/--output` is omitted. For known-device implementation prompts, it expects SEARCH/REPLACE output and patches the detected model file. With `--compile`, it builds the BoardRunner SDK model after applying the patch. With `--evaluate`, it appends per-call metrics to `fastdyn_llm_history/metrics.jsonl`.

If the LLM returns a VETO or routing-only JSON response, `fastdyn llm` writes `fastdyn_work/routing.json` but does not create files or edit TOML. Rerun trace analysis to consume that routing decision:

```bash
fastdyn trace-analyze -c configs/automatic_ardurover462.toml -o fastdyn_work --latest-run-dir fastdyn_recent_run
```

If that routing decision requires new files or TOML entries, rerun with `--apply-routing` to review and apply those changes interactively before generating the next implementation prompt.
