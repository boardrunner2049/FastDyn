FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Install base dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    python3 \
    python3-venv \
    python3-pip \
    pkg-config \
    libglib2.0-dev \
    libcjson-dev \
    libpixman-1-dev \
    libstlink-dev \
    ccache \
    cmake \
    ninja-build \
    universal-ctags \
    curl \
    wget \
    lsb-release \
    gnupg \
    sudo \
    rapidjson-dev \
    libopencv-dev \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    nlohmann-json3-dev \
    libfdt-dev \
    libusb-1.0-0-dev \
    libstlink-dev \
    unzip \
    openjdk-21-jdk \
    && rm -rf /var/lib/apt/lists/*

# Install Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Install Java 21 for Ghidra 12.0.4
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Install Gazebo Harmonic
RUN curl https://packages.osrfoundation.org/gazebo.gpg --output /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/gazebo-stable.list > /dev/null && \
    apt-get update && apt-get install -y gz-harmonic && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Copy everything from the parent directory into /workspace
# This includes FastDyn, libhw, qemu, banquo, etc.
COPY . /workspace

# Clone ArduPilot source code for static analysis
RUN mkdir -p source_code_ardupilot && \
    git clone https://github.com/ArduPilot/ardupilot.git source_code_ardupilot/ardupilot && \
    cd source_code_ardupilot/ardupilot && \
    git submodule update --init && \
    cd /workspace && \
    mkdir -p firmware_build_ardupilot && \
    cp -r source_code_ardupilot/ardupilot firmware_build_ardupilot/ardupilot

# Download and install Ghidra next to FastDyn
RUN wget https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.0.4_build/ghidra_12.0.4_PUBLIC_20260303.zip && \
    unzip ghidra_12.0.4_PUBLIC_20260303.zip && \
    rm ghidra_12.0.4_PUBLIC_20260303.zip

WORKDIR /workspace/FastDyn

# Remove the copied fastdyn-env so it can be cleanly recreated inside the container
# Also remove any CMake/Meson caches copied from the host machine that have hardcoded host paths
RUN rm -rf fastdyn-env && \
    find /workspace -name "CMakeCache.txt" -delete && \
    find /workspace -name "build.ninja" -delete && \
    find /workspace -name "meson-private" -type d -exec rm -rf {} +

# Run the setup script to build dependencies (QEMU, Gazebo deps, libhw, etc.)
RUN /bin/bash -c "source ./setup.sh --build-qemu --build-gazebo --skip-optifuzz"

# Build FastDyn
RUN /bin/bash -c "source fastdyn-env/bin/activate && make PROBE=true DEV=true LIBHW=true LIBGZ=true FLIGHT_CONTROLLERS=true DEBUG_PRINT=true LIBFUZZ=true"

# Automatically activate the virtual environment for interactive shells
# Also add libhw to the library path so the fastdyn plugin can find it
ENV PATH="/workspace/FastDyn/fastdyn-env/bin:${PATH}"
ENV LD_LIBRARY_PATH="/workspace/libhw/out"
ENV GHIDRA_INSTALL_DIR="/workspace/ghidra_12.0.4_PUBLIC"

# Set default command to bash
CMD ["/bin/bash"]
