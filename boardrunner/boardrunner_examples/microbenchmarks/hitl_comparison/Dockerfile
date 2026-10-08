# syntax=docker/dockerfile:1
#
# Figure 8c (HITL execution): Avatar2 vs FastDyn passthrough, both forwarding
# every MMIO access of the same firmware to a physical STM32F429I-DISC1.
# Built on the fastdyn-slice image (patched QEMU + FastDyn source), adds
# LibHW, a passthrough-enabled plugin build, and Avatar2 with avatar-qemu.
#
# BUILD (context = FastDyn repo root):
#     docker build -f boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison/Dockerfile \
#         -t hitl-slice:latest .
# RUN (board on USB):
#     docker run --rm -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw' \
#         -v "$PWD:/results" hitl-slice:latest check|flash|avatar2 10|fastdyn 10|all 10
ARG BASE=fastdyn-slice:latest
FROM ${BASE}

RUN apt-get update && apt-get install -y --no-install-recommends \
        libstlink-dev libusb-1.0-0-dev openocd gdb-multiarch python3-pip \
        libssl-dev patch \
    && rm -rf /var/lib/apt/lists/*

# ---- LibHW (ST-Link backend via libstlink) --------------------------------
ARG LIBHW_REF=6e53775
RUN git clone https://github.com/boardrunner2049/libhw /workspace/libhw \
    && cd /workspace/libhw && git checkout "$LIBHW_REF" && make \
    && test -f out/libhw.so

# ---- FastDyn plugin with passthrough (separate build dir) ------------------
# The hw_session patch drops the per-access halt query (a second USB
# round-trip) on the read/write happy path; see the README.
WORKDIR /workspace/FastDyn
COPY boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison \
     boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison
RUN patch -p1 < boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison/patches/fastdyn_hw_session_no_halt_check.patch \
    && meson setup build-hitl \
        -Dqemu_path=/workspace/qemu -Dlibhw_path=/workspace/libhw -Daflnet_path=/tmp/nonexistent \
        -Denable_libhw=true -Ddevice_models=true \
        -Denable_libgz=false -Denable_libfuzz=false -Denable_aflnet=false -Denable_probe=false \
        -DDEBUG_PRINT=false -Denable_libpy=false -Denable_sundials=false \
        -Denable_phy=false -Denable_fmu=false -Denable_flight_controllers=false \
    && ninja -C build-hitl && test -f build-hitl/libfastdyn.so

# ---- Avatar2 + avatar-qemu (pinned) ----------------------------------------
ARG AVATAR2_REF=56a90cfe73
ARG AVATAR_QEMU_REF=d774496465
RUN git clone https://github.com/avatartwo/avatar2 /workspace/avatar2 \
    && cd /workspace/avatar2 && git checkout "$AVATAR2_REF" \
    && git submodule update --init targets/src/avatar-qemu \
    && cd targets/src/avatar-qemu && git checkout "$AVATAR_QEMU_REF" \
    && patch -p1 < /workspace/FastDyn/boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison/patches/avatar_qemu_gcc13_softfloat.patch \
    && mkdir -p build && cd build \
    && ../configure --target-list=arm-softmmu --disable-werror --disable-docs \
                    --disable-bpf --disable-gtk --disable-vnc --disable-sdl \
    && make -j"$(nproc)" qemu-system-arm \
    && test -f qemu-system-arm
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir "setuptools<81" pygdbmi posix_ipc \
    && /opt/venv/bin/pip install --no-cache-dir -e /workspace/avatar2 \
    && /opt/venv/bin/python -P -c "from avatar2 import Avatar, OpenOCDTarget, QemuTarget"

COPY boardrunner/boardrunner_examples/microbenchmarks/hitl_comparison/run.sh /opt/bin/hitl.sh
RUN chmod +x /opt/bin/hitl.sh
ENTRYPOINT ["/opt/bin/hitl.sh"]
CMD ["check"]
