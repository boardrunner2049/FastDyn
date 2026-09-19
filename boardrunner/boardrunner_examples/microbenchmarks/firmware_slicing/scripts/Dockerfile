# syntax=docker/dockerfile:1
#
# FastDyn firmware-slicing microbenchmark — self-contained evaluator image.
#
# Builds patched QEMU (Arslan8/qemu@fastdyn + FastDyn icount patch), the
# FastDyn plugin (minimal: modifier + virtual dispatch paths, no PHY/FMU/
# FLIGHT_CONTROLLERS/LIBFUZZ), cross-compiles the STM32 slicing firmware,
# and ships the pinned per-bench config files + run scripts.
#
# BUILD (from the FastDyn repo root, which becomes the docker context):
#     cd /path/to/FastDyn
#     docker build \
#         -f boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing/scripts/Dockerfile \
#         -t fastdyn-slice:latest .
#
# Optional build args:
#     --build-arg N_INNER=1000000   # firmware loop bound (default 1000)
#     --build-arg QEMU_REF=fastdyn  # patched-QEMU git ref
#
# RUN (results land in ./results when it is mounted at /results):
#     mkdir -p results
#     docker run --rm -v "$PWD/results:/results" fastdyn-slice:latest both 1000
#     docker run --rm -v "$PWD/results:/results" fastdyn-slice:latest modifier 100
#     docker run --rm -v "$PWD/results:/results" fastdyn-slice:latest plot   # -> results/fig8.pdf
#
# TIP: create a `.dockerignore` at the FastDyn root before building to avoid
# copying `fastdyn-env/`, `build/`, `.git/`, `out/`, `fastdyn_work/` into the
# image. Sample lines:
#     fastdyn-env
#     build
#     .git
#     out
#     fastdyn_work

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C \
    LANG=C

# ----------------------------------------------------------------------------
# System dependencies
# ----------------------------------------------------------------------------
# Grouped so the layer cache invalidates only when this set changes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        # C/C++ toolchain + build system
        gcc g++ make ninja-build meson pkg-config git ca-certificates \
        # QEMU deps (arm-softmmu target, plugin-enabled, no GUI backends)
        libglib2.0-dev libpixman-1-dev libfdt-dev zlib1g-dev bzip2 \
        flex bison \
        # FastDyn plugin deps
        libcjson-dev \
        # Python: meson uses python3, QEMU meson also wants the python-dev
        # runtime pkgconfig even though we don't build any Python bindings.
        python3 python3-dev python3-venv \
        # ARM cross toolchain for the Cortex-M4 firmware (newlib provides
        # libc.a which the -nostdlib link still needs for AEABI helpers)
        gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi \
        # Runtime tools used by the bench scripts
        bash coreutils grep gawk \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# Patched QEMU
# ----------------------------------------------------------------------------
# Regenerated patch applies cleanly forward against the current fastdyn tip.
ARG QEMU_REPO=https://github.com/Arslan8/qemu.git
ARG QEMU_REF=fastdyn

WORKDIR /workspace
COPY patches/qemu-fastdyn-plugin-icount.patch /tmp/qemu-fastdyn-plugin-icount.patch
RUN git clone --depth=1 --branch "$QEMU_REF" "$QEMU_REPO" qemu \
    && cd qemu \
    && git apply /tmp/qemu-fastdyn-plugin-icount.patch \
    && rm /tmp/qemu-fastdyn-plugin-icount.patch \
    && mkdir -p build && cd build \
    && ../configure --target-list=arm-softmmu --enable-plugins \
                    --disable-docs --disable-gtk --disable-vnc \
                    --disable-sdl --disable-werror \
    && make -j"$(nproc)" qemu-system-arm

# QEMU RAM backing files that the plugin config's memory-backend-file
# entries expect. Sizes match rover462 / minimal / microbench configs.
RUN mkdir -p /workspace/qemu/ws \
    && truncate -s 512M /workspace/qemu/ws/my_m4_ram3 \
    && truncate -s 512K /workspace/qemu/ws/my_m4_ram

# matplotlib for `run.sh plot` (Figure 8 rendering inside the container)
RUN apt-get update && apt-get install -y --no-install-recommends python3-matplotlib \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# FastDyn source + plugin build
# ----------------------------------------------------------------------------
# Layout deliberately mirrors the host: FastDyn/ and qemu/ are siblings under
# /workspace so `run_bench.sh`'s relative `../qemu/build/qemu-system-arm`
# resolves without modification.
WORKDIR /workspace/FastDyn
COPY . /workspace/FastDyn/

# Skip the Makefile's `fetch` step (would run `git submodule update` which
# fails on a rsync-copied tree). Call meson/ninja directly with the same
# minimal feature set the microbench needs.
RUN rm -rf build \
    && meson setup build \
        -Dqemu_path=/workspace/qemu \
        -Dlibhw_path=/tmp/nonexistent \
        -Daflnet_path=/tmp/nonexistent \
        -Denable_libhw=false \
        -Denable_libgz=false \
        -Denable_libfuzz=false \
        -Denable_aflnet=false \
        -Denable_probe=false \
        -Ddevice_models=false \
        -DDEBUG_PRINT=false \
        -Denable_libpy=false \
        -Denable_sundials=false \
        -Denable_phy=false \
        -Denable_fmu=false \
        -Denable_flight_controllers=false \
    && ninja -C build \
    && test -f build/libfastdyn.so

# ----------------------------------------------------------------------------
# Firmware (cross-compiled)
# ----------------------------------------------------------------------------
# N_INNER=1000 matches the current host build. Override at build time for
# larger sweeps: --build-arg N_INNER=1000000.
ARG N_INNER=1000
ARG N_OUTER=1
RUN cd boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing/firmware \
    && make clean \
    && make N_INNER=${N_INNER} N_OUTER=${N_OUTER} \
    && arm-none-eabi-nm build/microbench_slice.axf \
       | grep -E "slice_probe|bench_start|bench_done"

# ----------------------------------------------------------------------------
# Entrypoint: scripts/run.sh (modifier | virtual | both | plot)
# ----------------------------------------------------------------------------
RUN install -D -m 0755 boardrunner/boardrunner_examples/microbenchmarks/firmware_slicing/scripts/run.sh /opt/bin/run.sh \
    && mkdir -p /results

WORKDIR /workspace/FastDyn
ENTRYPOINT ["/opt/bin/run.sh"]
CMD ["both", "100"]
