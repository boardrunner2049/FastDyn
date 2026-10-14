# FastDyn with the patched QEMU, libhw, the libFuzz fuzzer and the
# Gazebo/Courbet flight-controller backend, all built from this checkout.
#
# Before building, check out the submodules it builds from:
#   git submodule update --init --depth 1 \
#       third_party/common/qemu third_party/common/libhw \
#       third_party/common/cmsis-svd-data \
#       third_party/courbet_deps/mavlink_headers \
#       third_party/courbet_deps/ardupilot_gazebo third_party/courbet_deps/SITL_Models
# then build from the repository root:
#   docker build -t fastdyn .
#   docker run -it fastdyn
# The container starts in /workspace/FastDyn with the `fastdyn` command ready.
# Dockerfile.dockerignore keeps host build products and .git out of the image,
# so the build runs the steps setup.sh and the Makefile would run.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config cmake ninja-build flex bison git ca-certificates \
        curl wget lsb-release gnupg \
        python3 python3-venv python3-pip \
        libglib2.0-dev libcjson-dev libpixman-1-dev libfdt-dev \
        libstlink-dev libusb-1.0-0-dev \
        rapidjson-dev libopencv-dev nlohmann-json3-dev \
        libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*

# Rust, for the libFuzz fuzzer library
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Gazebo Harmonic, for the flight-controller backend
RUN curl https://packages.osrfoundation.org/gazebo.gpg --output /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/gazebo-stable.list && \
    apt-get update && apt-get install -y gz-harmonic && rm -rf /var/lib/apt/lists/*

COPY . /workspace/FastDyn
WORKDIR /workspace/FastDyn

# Python: FastDyn's requirements (meson among them) and FastDyn
ENV VIRTUAL_ENV=/workspace/FastDyn/fastdyn-env
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
RUN python3 -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e src

# The patched QEMU FastDyn runs in (FastDyn's patch applied to the pinned submodule)
RUN cd third_party/common/qemu && P=../../../patches/qemu-fastdyn-plugin-icount.patch \
    && { patch -p1 -R -s -f --dry-run < $P >/dev/null || patch -p1 < $P; } \
    && mkdir build && cd build \
    && ../configure --target-list=arm-softmmu --enable-plugins \
    && make -j"$(nproc)" qemu-system-arm

# libhw, and the Gazebo/Courbet parts the flight-controller backend links
RUN make -C third_party/common/libhw \
    && for d in third_party/courbet_deps/ardupilot_gazebo \
                virtuals/physics/flight_controllers/courbet/gazebo \
                virtuals/physics/physics_engines/gazebo; do \
           cmake -S "$d" -B "$d/build" -DCMAKE_BUILD_TYPE=RelWithDebInfo \
           && cmake --build "$d/build" -j"$(nproc)" || exit 1; \
       done

# FastDyn, with the options `make DEV=true LIBHW=true LIBGZ=true
# FLIGHT_CONTROLLERS=true DEBUG_PRINT=true LIBFUZZ=true` passes to meson
RUN meson setup build \
        -Dqemu_path="$PWD/third_party/common/qemu" \
        -Dlibhw_path="$PWD/third_party/common/libhw" \
        -Ddevice_models=true -Denable_libhw=true -Denable_libgz=true \
        -Denable_flight_controllers=true -Denable_phy=true \
        -Denable_libfuzz=true -DDEBUG_PRINT=true \
    && (cd virtuals/fuzzer/fastdyn_fuzz_lib && cargo build --release) \
    && ninja -C build \
    && fastdyn --help >/dev/null

ENV LD_LIBRARY_PATH="/workspace/FastDyn/third_party/common/libhw/out"
CMD ["/bin/bash"]
