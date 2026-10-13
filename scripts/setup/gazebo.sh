# The legacy Gazebo/Courbet backend: its CMake projects, libhw, and FastDyn
# built with LIBGZ/LIBHW. Sourced by setup.sh.

fastdyn_setup_gazebo_pkg_config() {
  local module

  if ! command -v pkg-config >/dev/null 2>&1; then
    echo "pkg-config was not found. Install pkg-config and Gazebo Harmonic development packages." >&2
    return 1
  fi

  for module in gz-transport13 gz-msgs10 protobuf; do
    if ! pkg-config --exists "$module"; then
      cat >&2 <<EOF
Missing Gazebo dependency: $module
Install Gazebo Harmonic first:
  bash virtuals/physics/flight_controllers/courbet/utils/install_gazebo_harmonic.sh
EOF
      return 1
    fi
  done
}

fastdyn_setup_gazebo_deps_build() {
  local repo_root="$1"

  fastdyn_setup_gazebo_pkg_config || return
  fastdyn_setup_cmake_build \
    "$repo_root/third_party/courbet_deps/ardupilot_gazebo" \
    "$repo_root/third_party/courbet_deps/ardupilot_gazebo/build" \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo || return
  fastdyn_setup_cmake_build \
    "$repo_root/virtuals/physics/flight_controllers/courbet/gazebo" \
    "$repo_root/virtuals/physics/flight_controllers/courbet/gazebo/build" || return
  fastdyn_setup_cmake_build \
    "$repo_root/virtuals/physics/physics_engines/gazebo" \
    "$repo_root/virtuals/physics/physics_engines/gazebo/build" || return

  local wrapper="$repo_root/virtuals/physics/physics_engines/gazebo/build/lib/libgz_wrapper.so"
  local services="$repo_root/virtuals/physics/flight_controllers/courbet/gazebo/build/services"
  if [[ ! -f "$wrapper" ]]; then
    echo "Gazebo wrapper build did not produce $wrapper" >&2
    return 1
  fi
  if [[ ! -x "$services" ]]; then
    echo "Courbet Gazebo services build did not produce $services" >&2
    return 1
  fi
}

fastdyn_setup_gazebo_plugin_build() {
  local repo_root="$1"
  local qemu_root="$2"
  local libhw_root="$3"
  local qemu_bin="$qemu_root/build/qemu-system-arm"

  if [[ ! -x "$qemu_bin" ]]; then
    cat >&2 <<EOF
cannot build FastDyn legacy Gazebo plugin because patched QEMU is missing:
  $qemu_bin

Build it first with:
  source "$repo_root/setup.sh" --build-qemu --build-gazebo --skip-optifuzz
EOF
    return 1
  fi

  fastdyn_setup_gazebo_deps_build "$repo_root" || return
  fastdyn_setup_libhw_build "$repo_root" "$libhw_root" || return
  fastdyn_setup_cjson "$repo_root" || return
  make -C "$repo_root" \
    qemu_path="$qemu_root" \
    libhw_path="$libhw_root" \
    LIBHW=true \
    LIBGZ=true \
    FLIGHT_CONTROLLERS=true \
    DEV=true \
    DEBUG_PRINT=true || return
}
