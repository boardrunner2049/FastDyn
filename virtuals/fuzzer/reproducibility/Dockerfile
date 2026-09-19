# syntax=docker/dockerfile:1
# Build from the FastDyn repository root:
#   docker build -f virtuals/fuzzer/reproducibility/Dockerfile \
#     -t fastdyn-fuzzer-repro .
#
# The image builds both FastDyn variants used by the reproducibility launchers:
#   * build/         - normal coverage and ST-LINK/Elder runs
#   * build-aflnet/  - in-process AFLNet campaigns

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG BUILD_JOBS=4

# These arguments make dependency updates explicit while the defaults retain
# the versions used by the checked-in reproduction setup.
ARG QEMU_REPOSITORY=https://github.com/Arslan8/qemu.git
ARG QEMU_COMMIT=99b54836ded8dc3271eba52deee7453efc0a13b9
ARG LIBHW_REPOSITORY=https://github.com/PSecLab/libhw.git
ARG LIBHW_COMMIT=6e53775f8bc3ce932623a5019c2462d0de987e66
ARG AFLNET_REPOSITORY=https://github.com/CSmith1539/aflnet.git
ARG AFLNET_COMMIT=509a10384d53ce6dd6f04c6870454619dbf7d35e

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/fastdyn-venv \
    PATH=/opt/fastdyn-venv/bin:/workspace/qemu/build:/workspace/aflnet:${PATH} \
    LD_LIBRARY_PATH=/workspace/FastDyn/build:/workspace/FastDyn/build-aflnet:/workspace/libhw/out

# Dependencies for the normal FastDyn/ST-LINK build, patched QEMU, AFLNet,
# and each host-side program that the reproduction launchers check for.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash binutils build-essential ca-certificates cmake curl dosfstools \
        file git iproute2 iputils-ping libcjson-dev libcap-dev libfdt-dev \
        libglib2.0-dev libgraphviz-dev libpixman-1-dev libslirp-dev \
        libstlink-dev libusb-1.0-0-dev meson mosquitto mosquitto-clients \
        netcat-openbsd ninja-build openocd pkg-config python3 python3-dev \
        python3-pip python3-venv socat zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Docker's context is FastDyn/. Deliberately stage only the sources and
# artifacts consumed by the reproducibility builds and launchers. In
# particular, do not copy unrelated virtual platforms, existing build trees,
# or the multi-gigabyte LibAFL build cache.
COPY meson.build meson_options.txt config.h.in requirements.txt /workspace/FastDyn/
COPY include/ /workspace/FastDyn/include/
COPY core/ /workspace/FastDyn/core/
COPY utils/ /workspace/FastDyn/utils/
COPY device_models/ /workspace/FastDyn/device_models/
COPY src/ /workspace/FastDyn/src/
COPY patches/qemu-fastdyn-plugin-icount.patch /workspace/FastDyn/patches/

# Sources unconditionally included by FastDyn's virtuals build.
COPY virtuals/meson.build virtuals/virtuals.c virtuals/runtime_sdk.c /workspace/FastDyn/virtuals/
COPY virtuals/introspection/ /workspace/FastDyn/virtuals/introspection/
COPY virtuals/function_counter/ /workspace/FastDyn/virtuals/function_counter/
COPY virtuals/function_tracer/ /workspace/FastDyn/virtuals/function_tracer/
COPY virtuals/object_sanitizer/ /workspace/FastDyn/virtuals/object_sanitizer/
COPY virtuals/variable_watch/ /workspace/FastDyn/virtuals/variable_watch/
COPY virtuals/world/ /workspace/FastDyn/virtuals/world/
# virtuals.c always includes this public physics interface; the physics
# implementation itself remains excluded because enable_phy is off.
COPY virtuals/physics/phy.h /workspace/FastDyn/virtuals/physics/

# AFLNet plugin sources plus the self-contained reproduction launchers,
# configs, models, and seed corpora. The LibAFL backend is not part of the
# published coverage flow and is therefore intentionally excluded.
COPY virtuals/fuzzer/meson.build \
     virtuals/fuzzer/fuzz.c \
     virtuals/fuzzer/fuzz_aflnet.c \
     virtuals/fuzzer/cov_bbl.c \
     virtuals/fuzzer/cov_trace.c \
     virtuals/fuzzer/fuzz.h \
     virtuals/fuzzer/cov_bbl.h \
     virtuals/fuzzer/cov_trace.h \
     virtuals/fuzzer/run_aflnet_coverage.sh \
     virtuals/fuzzer/run_board_coverage.sh \
     virtuals/fuzzer/run_board_target.sh \
     virtuals/fuzzer/run_qemu_coverage.sh \
     /workspace/FastDyn/virtuals/fuzzer/
COPY virtuals/fuzzer/protocol_fuzzers/ /workspace/FastDyn/virtuals/fuzzer/protocol_fuzzers/
COPY virtuals/fuzzer/stateful_fuzzers/ /workspace/FastDyn/virtuals/fuzzer/stateful_fuzzers/
COPY virtuals/fuzzer/schema/schema.h /workspace/FastDyn/virtuals/fuzzer/schema/
COPY virtuals/fuzzer/reproducibility/configs/ /workspace/FastDyn/virtuals/fuzzer/reproducibility/configs/
COPY virtuals/fuzzer/reproducibility/models/ /workspace/FastDyn/virtuals/fuzzer/reproducibility/models/
COPY virtuals/fuzzer/reproducibility/scripts/ /workspace/FastDyn/virtuals/fuzzer/reproducibility/scripts/
COPY virtuals/fuzzer/reproducibility/seeds/ /workspace/FastDyn/virtuals/fuzzer/reproducibility/seeds/

# BoardRunner's runtime and exactly the four firmware examples used by the
# coverage launchers (MQTT, lwIP, SD-card, and Modbus).
COPY boardrunner/boardrunner_sdk/ /workspace/FastDyn/boardrunner/boardrunner_sdk/
COPY boardrunner/boardrunner_examples/examples/STM32H753zi/MQTT/ /workspace/FastDyn/boardrunner/boardrunner_examples/examples/STM32H753zi/MQTT/
COPY boardrunner/boardrunner_examples/examples/STM32F769i-eval/LWIP/ /workspace/FastDyn/boardrunner/boardrunner_examples/examples/STM32F769i-eval/LWIP/
COPY boardrunner/boardrunner_examples/examples/STM32F769i-eval/SD_Card/ /workspace/FastDyn/boardrunner/boardrunner_examples/examples/STM32F769i-eval/SD_Card/
COPY boardrunner/boardrunner_examples/examples/Nucleo-F103RB/Modbus/ /workspace/FastDyn/boardrunner/boardrunner_examples/examples/Nucleo-F103RB/Modbus/

# Required only by the documented OpenOCD/physical-board preparation flow.
COPY tests/idle_firmwares/prebuilt/ /workspace/FastDyn/tests/idle_firmwares/prebuilt/

RUN set -eux; \
    git init /workspace/qemu; \
    git -C /workspace/qemu remote add origin "$QEMU_REPOSITORY"; \
    git -C /workspace/qemu fetch --depth 1 origin "$QEMU_COMMIT"; \
    git -C /workspace/qemu checkout --detach FETCH_HEAD; \
    git -C /workspace/qemu apply /workspace/FastDyn/patches/qemu-fastdyn-plugin-icount.patch; \
    mkdir -p /workspace/qemu/ws /workspace/qemu/build; \
    truncate -s 512M /workspace/qemu/ws/my_m4_ram3; \
    truncate -s 512K /workspace/qemu/ws/my_m4_ram; \
    cd /workspace/qemu/build; \
    ../configure --target-list=arm-softmmu --enable-plugins; \
    make -j"$BUILD_JOBS" qemu-system-arm

RUN set -eux; \
    git init /workspace/libhw; \
    git -C /workspace/libhw remote add origin "$LIBHW_REPOSITORY"; \
    git -C /workspace/libhw fetch --depth 1 origin "$LIBHW_COMMIT"; \
    git -C /workspace/libhw checkout --detach FETCH_HEAD; \
    make -C /workspace/libhw -j"$BUILD_JOBS"

RUN set -eux; \
    git init /workspace/aflnet; \
    git -C /workspace/aflnet remote add origin "$AFLNET_REPOSITORY"; \
    git -C /workspace/aflnet fetch --depth 1 origin "$AFLNET_COMMIT"; \
    git -C /workspace/aflnet checkout --detach FETCH_HEAD; \
    make -C /workspace/aflnet -j"$BUILD_JOBS" afl-fuzz libaflnet.a

RUN python3 -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir --upgrade pip \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir -r /workspace/FastDyn/requirements.txt \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir /workspace/FastDyn/src \
    && "$VIRTUAL_ENV/bin/python" -c 'from pathlib import Path; from fastdyn.fastdyn_log import DEFAULT_LOG_CONFIG; assert Path(DEFAULT_LOG_CONFIG).is_file(), DEFAULT_LOG_CONFIG' \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir 'pymodbus>=3.0' pyserial

# Normal runtime: the ST-LINK/Elder backend is required for physical board
# runs and also supports the software-only Modbus baseline. Build the
# AFLNet coverage backend into this variant as well: fuzzing=false leaves
# AFLNet dormant, while its shared BBL collector is needed by the normal
# QEMU-only and Elder coverage launchers.
RUN meson setup /workspace/FastDyn/build /workspace/FastDyn \
        -Dqemu_path=/workspace/qemu \
        -Dlibhw_path=/workspace/libhw \
        -Daflnet_path=/workspace/aflnet \
        -Denable_libhw=true \
        -Denable_aflnet=true \
        -Ddevice_models=true \
    && ninja -C /workspace/FastDyn/build -j"$BUILD_JOBS" \
    && nm -D /workspace/FastDyn/build/libfastdyn.so | grep -q ' T fuzz_bbl_init$'

RUN cmake -S /workspace/FastDyn/boardrunner/boardrunner_sdk \
          -B /workspace/FastDyn/boardrunner/boardrunner_sdk/build \
          -DFASTDYN_INCLUDE_DIR=/workspace/FastDyn/include \
          -DQEMU_INCLUDE_DIR=/workspace/qemu/include \
    && cmake --build /workspace/FastDyn/boardrunner/boardrunner_sdk/build \
          --parallel "$BUILD_JOBS"

# AFLNet has a dedicated plugin build and matching BoardRunner VIO library.
# The reproduced protocols boot through Elder device models, so this variant
# needs the dispatcher and libhw-backed Elder sources even though campaigns do
# not access physical hardware.
RUN meson setup /workspace/FastDyn/build-aflnet /workspace/FastDyn \
        -Dqemu_path=/workspace/qemu \
        -Daflnet_path=/workspace/aflnet \
        -Dlibhw_path=/workspace/libhw \
        -Denable_aflnet=true \
        -Denable_libfuzz=false \
        -Denable_libhw=true \
        -Ddevice_models=true \
    && ninja -C /workspace/FastDyn/build-aflnet -j"$BUILD_JOBS" \
    && nm -D /workspace/FastDyn/build-aflnet/libfastdyn.so | grep -q ' T dev_init$' \
    && nm -D /workspace/FastDyn/build-aflnet/libfastdyn.so | grep -q ' elder_model_def$'

RUN cmake -S /workspace/FastDyn/boardrunner/boardrunner_sdk \
          -B /workspace/FastDyn/boardrunner/boardrunner_sdk/build-aflnet \
          -DFASTDYN_INCLUDE_DIR=/workspace/FastDyn/include \
          -DQEMU_INCLUDE_DIR=/workspace/qemu/include \
    && cmake --build /workspace/FastDyn/boardrunner/boardrunner_sdk/build-aflnet \
          --parallel "$BUILD_JOBS"

ENV FASTDYN_BOARDRUNNER=/opt/fastdyn-venv/bin/boardrunner

WORKDIR /workspace/FastDyn
CMD ["/bin/bash"]
