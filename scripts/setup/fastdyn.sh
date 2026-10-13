# The FastDyn QEMU plugin (build/libfastdyn.so) and the cJSON it needs.
# Sourced by setup.sh.

fastdyn_setup_cjson() {
  local repo_root="$1"
  if pkg-config --exists libcjson; then
    return 0
  fi

  if ! command -v cmake >/dev/null 2>&1; then
    echo "libcjson development files were not found and cmake is unavailable to build a local copy." >&2
    echo "Install libcjson-dev, or install cmake and rerun setup." >&2
    return 1
  fi

  local dep_root="$repo_root/out/deps/cjson"
  local source_dir="$dep_root/src"
  local build_dir="$dep_root/build"
  local install_dir="$dep_root/install"

  if [[ ! -d "$source_dir/.git" ]]; then
    rm -rf "$source_dir"
    git clone --depth 1 --branch v1.7.18 https://github.com/DaveGamble/cJSON.git "$source_dir" || return
  fi

  if [[ ! -f "$install_dir/lib/pkgconfig/libcjson.pc" && ! -f "$install_dir/lib64/pkgconfig/libcjson.pc" ]]; then
    cmake -S "$source_dir" -B "$build_dir" \
      -DENABLE_CJSON_TEST=Off \
      -DENABLE_CJSON_UTILS=Off \
      -DCMAKE_INSTALL_PREFIX="$install_dir" || return
    cmake --build "$build_dir" --target install || return
  fi

  local pkg_paths=(
    "$install_dir/lib/pkgconfig"
    "$install_dir/lib64/pkgconfig"
  )
  local lib_paths=(
    "$install_dir/lib"
    "$install_dir/lib64"
  )
  local path
  for path in "${pkg_paths[@]}"; do
    if [[ -d "$path" ]]; then
      export PKG_CONFIG_PATH="$path${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    fi
  done
  for path in "${lib_paths[@]}"; do
    if [[ -d "$path" ]]; then
      export LD_LIBRARY_PATH="$path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
  done

  if ! pkg-config --exists libcjson; then
    echo "local cJSON build did not provide libcjson.pc under $install_dir" >&2
    return 1
  fi
}

# The make arguments setup builds FastDyn with: device models, the physics
# engine with the FMU and flight-controller backends, and libhw (passthrough,
# twintrace recording) when it is built.
fastdyn_setup_plugin_make_args() {
  local qemu_root="$1"
  local libhw_root="$2"

  printf '%s\n' "qemu_path=$qemu_root" DEV=true PHY=true FLIGHT_CONTROLLERS=true FMU=true
  if [[ -f "$libhw_root/out/libhw.so" ]]; then
    printf '%s\n' "libhw_path=$libhw_root" LIBHW=true
  fi
}

fastdyn_setup_plugin_build() {
  local repo_root="$1"
  local qemu_root="$2"
  local libhw_root="$3"
  local qemu_bin="$qemu_root/build/qemu-system-arm"

  if [[ ! -x "$qemu_bin" ]]; then
    echo "cannot build FastDyn QEMU plugin because patched QEMU is missing: $qemu_bin" >&2
    return 1
  fi

  local make_args
  mapfile -t make_args < <(fastdyn_setup_plugin_make_args "$qemu_root" "$libhw_root")
  fastdyn_setup_cjson "$repo_root" || return
  make -C "$repo_root" "${make_args[@]}" || return
}

fastdyn_setup_plugin_hint() {
  local repo_root="$1"
  local qemu_root="$2"
  local libhw_root="$3"
  local plugin="$repo_root/build/libfastdyn.so"

  if [[ -f "$plugin" ]]; then
    echo "FastDyn QEMU plugin is ready: $plugin"
    return 0
  fi

  local make_args
  mapfile -t make_args < <(fastdyn_setup_plugin_make_args "$qemu_root" "$libhw_root")
  cat >&2 <<EOF
FastDyn QEMU plugin is missing: $plugin
Build it after patched QEMU is ready with:
  make -C "$repo_root" ${make_args[*]}
EOF
}
