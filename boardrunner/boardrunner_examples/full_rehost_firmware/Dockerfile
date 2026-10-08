# syntax=docker/dockerfile:1
#
# pseclab/boardrunner: the fully rehosted NUCLEO-F103RB SDK workloads
# (Tables 5/7) and the naive-baseline comparison of Table 6. Built on the
# fastdyn-slice image (patched QEMU); adds LibHW, the FastDyn plugin with
# device models + passthrough + the coverage (basic-block) counter, the
# fastdyn CLI, the BoardRunner VIO SDK, and all generated models, firmware,
# hardware traces and replay tapes of this directory.
#
# BUILD (context = FastDyn repo root):
#     docker build -f boardrunner/boardrunner_examples/full_rehost_firmware/Dockerfile \
#         -t boardrunner:latest .
# RUN: see README.md (`docker run --rm -v "$PWD:/results" boardrunner:latest table6 all`)
ARG BASE=fastdyn-slice:latest
FROM ${BASE}

RUN apt-get update && apt-get install -y --no-install-recommends \
        libstlink-dev libusb-1.0-0-dev openocd cmake socat curl ca-certificates \
        python3-pip python3-venv universal-ctags \
    && rm -rf /var/lib/apt/lists/*

# Rust, for the LibAFL coverage module that hosts the basic-block counter.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal -q
ENV PATH="/root/.cargo/bin:${PATH}"

# ---- LibHW (ST-Link backend) ---------------------------------------------
ARG LIBHW_REF=6e53775
RUN git clone https://github.com/boardrunner2049/libhw /workspace/libhw \
    && cd /workspace/libhw && git checkout "$LIBHW_REF" && make && test -f out/libhw.so

# ---- FastDyn tree (fresh copy), CLI, plugin ------------------------------
WORKDIR /workspace/FastDyn
COPY . /workspace/FastDyn/
RUN python3 -m venv /opt/venv \
    && grep -vE '^(MAVProxy|pymavlink|pyghidra|jpype1|ollama|meson|ninja)' requirements.txt > /tmp/req.txt \
    && /opt/venv/bin/pip install --no-cache-dir -q -r /tmp/req.txt \
    && /opt/venv/bin/pip install --no-cache-dir -q -e src \
    && /opt/venv/bin/fastdyn --help >/dev/null
ENV PATH="/opt/venv/bin:${PATH}"

RUN cd virtuals/fuzzer/fastdyn_fuzz_lib && cargo build --release -q 2>&1 | tail -3 \
    && test -f target/release/libfastdyn_fuzzer.so -o -f target/release/libfastdyn_fuzzer.a
RUN rm -rf build && meson setup build \
        -Dqemu_path=/workspace/qemu -Dlibhw_path=/workspace/libhw -Daflnet_path=/tmp/nonexistent \
        -Denable_libhw=true -Ddevice_models=true -Denable_libfuzz=true \
        -Denable_libgz=false -Denable_aflnet=false -Denable_probe=false \
        -DDEBUG_PRINT=true -Denable_libpy=false -Denable_sundials=false \
        -Denable_phy=false -Denable_fmu=false -Denable_flight_controllers=false \
    && ninja -C build && cp /workspace/libhw/out/libhw.so build/ && test -f build/libfastdyn.so
ENV LD_LIBRARY_PATH="/workspace/FastDyn/build:/workspace/libhw/out:/workspace/FastDyn/virtuals/fuzzer/fastdyn_fuzz_lib/target/release:/workspace/FastDyn/device_models/postmartem/verifier"

# ---- BoardRunner VIO SDK + the models of one example (rebuilt per run) -----
RUN cp boardrunner/boardrunner_examples/full_rehost_firmware/Nucleo-F103RB/GPIO_with_GPIO_INT/generated_models/*.c \
       boardrunner/boardrunner_sdk/model/ \
    && cmake -S boardrunner/boardrunner_sdk -B boardrunner/boardrunner_sdk/build \
        -DFASTDYN_INCLUDE_DIR=/workspace/FastDyn/include -DQEMU_INCLUDE_DIR=/workspace/qemu/include >/dev/null \
    && cmake --build boardrunner/boardrunner_sdk/build -j >/dev/null

# qemu-system-arm must be on PATH for the fastdyn CLI (configs use qemu_path = "qemu-system-arm")
RUN ln -sf /workspace/qemu/build/qemu-system-arm /usr/local/bin/qemu-system-arm && mkdir -p /results

COPY boardrunner/boardrunner_examples/full_rehost_firmware/run.sh /opt/bin/boardrunner.sh
RUN chmod +x /opt/bin/boardrunner.sh
ENTRYPOINT ["/opt/bin/boardrunner.sh"]
CMD ["help"]
