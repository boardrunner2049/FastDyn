FROM ubuntu:24.04

# Rando dependencies
RUN apt-get update && apt-get install -y \
    vim \
    curl \
    ninja-build \
    flex \
    bison \
    libglib2.0-dev \
    libpixman-1-dev \
    build-essential \ 
    git \
    lsb-release \
    python3 \
    python3-venv \ 
    python3-pip \
    libstdc++6 \
    libprotobuf-dev \
    protobuf-compiler \
    libfdt1 \ 
    libdw1 \
    rapidjson-dev \
    libopencv-dev \
    libgstreamer1.0-dev \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libgstreamer-plugins-base1.0-dev \
    gstreamer1.0-tools

# Rustup
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Gazebo
RUN curl https://packages.osrfoundation.org/gazebo.gpg --output /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/gazebo-stable.list > /dev/null && \
    apt-get update && \
    apt-get install -y gz-harmonic

WORKDIR /root

# Clone all repos
RUN git clone https://github.com/boardrunner2049/qemu.git
RUN git clone https://github.com/michaelprooney/banquo.git
RUN git clone https://github.com/ArduPilot/ardupilot_gazebo.git
RUN git clone https://github.com/michaelprooney/SITL_Models.git

# Setup Qemu
WORKDIR /root/qemu
RUN git checkout fastdyn && \
    mkdir -p ws/memory && \
    touch ws/memory/my_m4_ram3 && \
    mkdir build && \
    cd build && \
    ../configure --target-list=arm-softmmu --enable-debug && \
    make qemu-system-arm

# Setup Banquo
WORKDIR /root/banquo/
RUN git checkout banquo-parser-impl && \
    cd banquo-parser && \
    cargo build --release

# Setup ardupilot_gazebo
ENV GZ_VERSION="harmonic"
WORKDIR /root/ardupilot_gazebo
RUN mkdir build && \
    cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=RelWithDebInfo && \
    make -j4
ENV GZ_SIM_SYSTEM_PLUGIN_PATH="/root/ardupilot_gazebo/build"
ENV GZ_SIM_RESOURCE_PATH="/root/ardupilot_gazebo/models:/root/ardupilot_gazebo/worlds"
ENV GZ_SIM_RESOURCE_PATH="/root/SITL_Models/Gazebo/models:/root/SITL_Models/Gazebo/worlds:${GZ_SIM_RESOURCE_PATH}"

# The Dyn™
# TODO: We have to configure everything in FastDyn!
# make in Fastdyn/, services, and maybe somewhere else I'm forgetting
WORKDIR /root/FastDyn
COPY FastDyn/virtuals virtuals/ 
COPY FastDyn/build build/

# Copy the plane and rover scripts into qemu/
WORKDIR /root/qemu
RUN cp /root/FastDyn/virtuals/physics/flight_controllers/courbet/scripts/planev462.sh ./ && \
    cp /root/FastDyn/virtuals/physics/flight_controllers/courbet/scripts/roverv462.sh ./

# Setup python dependencies
WORKDIR /root/FastDyn/virtuals/fuzzer/libafl_phi
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip && pip install -r requirements.txt

ENV LD_LIBRARY_PATH="/root/FastDyn/virtuals/physics/physics_engines/gazebo/build/lib"

RUN cargo clean && cargo build
CMD ["bash"]