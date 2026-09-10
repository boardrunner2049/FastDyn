#!/usr/bin/env bash

fastdyn_setup_usage() {
  cat <<'USAGE'
Usage: source ./setup.sh [OPTIONS]
       ./setup.sh [OPTIONS]

Create/update the FastDyn Python environment and local runtime workspace.

Options:
  --venv PATH           Virtual environment path (default: fastdyn-env)
  --python PYTHON       Python executable used to create the venv (default: python3)
  --build-qemu          Clone/build the patched QEMU fork and FastDyn QEMU plugin
  --build-gazebo        Build legacy Gazebo/Courbet deps and FastDyn with LIBGZ/LIBHW
  --qemu-root PATH      Patched QEMU checkout/workspace root (default: ../qemu)
  --qemu-repo URL       Patched QEMU repository (default: https://github.com/Arslan8/qemu.git)
  --qemu-ref REF        Patched QEMU ref/branch (default: fastdyn)
  --libhw-root PATH     libhw checkout/workspace root (default: ../libhw)
  --optifuzz-root PATH  Banquo checkout root for OptiFuzz (default: ../banquo)
  --optifuzz-repo URL   Banquo repository (default: https://github.com/michaelprooney/banquo.git)
  --optifuzz-ref REF    Banquo ref/branch (default: banquo-parser-impl)
  --with-rumoca         Initialize/build pinned Rumoca for FMU generation and lockstep viewing
  --skip-optifuzz       Do not create/update the Banquo dependency checkout for OptiFuzz
  --skip-rumoca         Do not initialize/build pinned Rumoca for FMU generation and lockstep viewing
  --skip-qemu-workspace Do not create the default QEMU RAM backing files
  --skip-submodules     Do not initialize Git submodules
  --no-upgrade-pip      Do not upgrade pip before installing dependencies
  -h, --help            Show this help

When sourced, this script activates the virtual environment after setup.
When executed, it prints the activation command.
USAGE
}

fastdyn_setup_qemu_patch() {
  local repo_root="$1"
  local qemu_root="$2"
  local patch="$repo_root/patches/qemu-fastdyn-plugin-icount.patch"

  if [[ ! -f "$patch" ]]; then
    echo "missing QEMU compatibility patch: $patch" >&2
    return 1
  fi

  if git -C "$qemu_root" apply --reverse --check "$patch" >/dev/null 2>&1; then
    echo "FastDyn QEMU compatibility patch is already applied"
    return 0
  fi

  if git -C "$qemu_root" apply --check "$patch" >/dev/null 2>&1; then
    git -C "$qemu_root" apply "$patch" || return
    echo "Applied FastDyn QEMU compatibility patch: $patch"
    return 2
  fi

  echo "FastDyn QEMU compatibility patch does not apply cleanly: $patch" >&2
  echo "Check that --qemu-ref points at the expected patched QEMU base." >&2
  return 1
}

fastdyn_setup_qemu_workspace() {
  local qemu_root="$1"
  local qemu_ws="$qemu_root/ws"

  mkdir -p "$qemu_ws" || return
  truncate -s 512M "$qemu_ws/my_m4_ram3" || return
  truncate -s 512K "$qemu_ws/my_m4_ram" || return

  echo "FastDyn QEMU RAM backing files are ready: $qemu_ws"
}

fastdyn_setup_qemu_hint() {
  local repo_root="$1"
  local qemu_root="$2"
  local qemu_repo="$3"
  local qemu_ref="$4"
  local qemu_bin="$qemu_root/build/qemu-system-arm"
  local qemu_patch="$repo_root/patches/qemu-fastdyn-plugin-icount.patch"

  if [[ -x "$qemu_bin" ]]; then
    if [[ -d "$qemu_root/.git" ]] &&
       ! git -C "$qemu_root" apply --reverse --check "$qemu_patch" >/dev/null 2>&1; then
      cat >&2 <<EOF
FastDyn patched QEMU exists, but the required icount/plugin compatibility patch
is not detected in the checkout:
  $qemu_bin

Apply it and rebuild with:
  source "$repo_root/setup.sh" --build-qemu --qemu-root "$qemu_root"
EOF
      return 0
    fi
    echo "FastDyn patched QEMU is ready: $qemu_bin"
    return 0
  fi

  cat >&2 <<EOF
FastDyn patched QEMU is missing: $qemu_bin
Build it with:
  mkdir -p "$qemu_root"
  git -C "$qemu_root" init
  git -C "$qemu_root" remote add origin "$qemu_repo" 2>/dev/null || git -C "$qemu_root" remote set-url origin "$qemu_repo"
  git -C "$qemu_root" fetch origin "$qemu_ref"
  git -C "$qemu_root" checkout -B fastdyn FETCH_HEAD
  git -C "$qemu_root" apply "$repo_root/patches/qemu-fastdyn-plugin-icount.patch"
  mkdir -p "$qemu_root/build"
  cd "$qemu_root/build"
  ../configure --target-list=arm-softmmu --enable-plugins
  make qemu-system-arm
EOF
}

fastdyn_setup_qemu_build() {
  local repo_root="$1"
  local qemu_root="$2"
  local qemu_repo="$3"
  local qemu_ref="$4"
  local qemu_bin="$qemu_root/build/qemu-system-arm"

  if [[ -x "$qemu_bin" && -d "$qemu_root/.git" ]]; then
    if fastdyn_setup_qemu_patch "$repo_root" "$qemu_root"; then
      echo "FastDyn patched QEMU is already built: $qemu_bin"
      return 0
    fi
    local patch_status=$?
    if [[ $patch_status -ne 2 ]]; then
      return "$patch_status"
    fi
    echo "QEMU patch changed; rebuilding $qemu_bin"
  elif [[ -x "$qemu_bin" ]]; then
    echo "FastDyn patched QEMU is already built: $qemu_bin"
    return 0
  fi

  mkdir -p "$qemu_root" || return
  git -C "$qemu_root" init || return
  git -C "$qemu_root" remote add origin "$qemu_repo" 2>/dev/null \
    || git -C "$qemu_root" remote set-url origin "$qemu_repo" \
    || return
  git -C "$qemu_root" fetch --depth 1 origin "$qemu_ref" || return
  git -C "$qemu_root" checkout -B fastdyn FETCH_HEAD || return
  fastdyn_setup_qemu_patch "$repo_root" "$qemu_root"
  local patch_status=$?
  if [[ $patch_status -ne 0 && $patch_status -ne 2 ]]; then
    return "$patch_status"
  fi
  mkdir -p "$qemu_root/build" || return
  if [[ ! -f "$qemu_root/build/build.ninja" ]]; then
    (cd "$qemu_root/build" && ../configure --target-list=arm-softmmu --enable-plugins) || return
  fi
  ninja -C "$qemu_root/build" -j"$(nproc)" qemu-system-arm || return
}

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

fastdyn_setup_plugin_build() {
  local repo_root="$1"
  local qemu_root="$2"
  local qemu_bin="$qemu_root/build/qemu-system-arm"

  if [[ ! -x "$qemu_bin" ]]; then
    echo "cannot build FastDyn QEMU plugin because patched QEMU is missing: $qemu_bin" >&2
    return 1
  fi

  fastdyn_setup_cjson "$repo_root" || return
  make -C "$repo_root" qemu_path="$qemu_root" PHY=true FLIGHT_CONTROLLERS=true FMU=true || return
}

fastdyn_setup_plugin_hint() {
  local repo_root="$1"
  local qemu_root="$2"
  local plugin="$repo_root/build/libfastdyn.so"

  if [[ -f "$plugin" ]]; then
    echo "FastDyn QEMU plugin is ready: $plugin"
    return 0
  fi

  cat >&2 <<EOF
FastDyn QEMU plugin is missing: $plugin
Build it after patched QEMU is ready with:
  make -C "$repo_root" qemu_path="$qemu_root" PHY=true FLIGHT_CONTROLLERS=true FMU=true
EOF
}

fastdyn_setup_build_jobs() {
  if command -v nproc >/dev/null 2>&1; then
    nproc
  else
    echo 1
  fi
}

fastdyn_setup_cmake_build() {
  local source_dir="$1"
  local build_dir="$2"
  shift 2

  if ! command -v cmake >/dev/null 2>&1; then
    echo "cmake was not found. Install cmake to build the legacy Gazebo dependencies." >&2
    return 1
  fi
  if [[ ! -f "$source_dir/CMakeLists.txt" ]]; then
    echo "missing CMake project: $source_dir" >&2
    return 1
  fi

  cmake -S "$source_dir" -B "$build_dir" "$@" || return
  cmake --build "$build_dir" -j"$(fastdyn_setup_build_jobs)" || return
}

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

fastdyn_setup_libhw_build() {
  local libhw_root="$1"
  local libhw_so="$libhw_root/out/libhw.so"

  if [[ ! -f "$libhw_root/Makefile" ]]; then
    cat >&2 <<EOF
libhw checkout is missing or incomplete: $libhw_root
Use --libhw-root to point at an existing checkout, or clone/build libhw there.
EOF
    return 1
  fi

  make -C "$libhw_root" || return

  if [[ ! -f "$libhw_so" ]]; then
    echo "libhw build did not produce $libhw_so" >&2
    return 1
  fi
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
  fastdyn_setup_libhw_build "$libhw_root" || return
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

fastdyn_setup_optifuzz_deps() {
  local banquo_root="$1"
  local banquo_repo="$2"
  local banquo_ref="$3"

  if ! command -v cargo >/dev/null 2>&1; then
    echo "cargo was not found. Install Rust/Cargo or rerun with --skip-optifuzz." >&2
    return 1
  fi

  if [[ ! -d "$banquo_root" ]]; then
    git clone --branch "$banquo_ref" "$banquo_repo" "$banquo_root" || return
  elif [[ -d "$banquo_root/.git" ]]; then
    if [[ ! -f "$banquo_root/banquo/Cargo.toml" || ! -f "$banquo_root/banquo-parser/Cargo.toml" ]]; then
      git -C "$banquo_root" fetch origin "$banquo_ref" || return
      git -C "$banquo_root" checkout "$banquo_ref" || return
    fi
  fi

  if [[ ! -f "$banquo_root/banquo/Cargo.toml" || ! -f "$banquo_root/banquo-parser/Cargo.toml" ]]; then
    cat >&2 <<EOF
OptiFuzz requires the Banquo checkout at:
  $banquo_root

Expected to find:
  $banquo_root/banquo/Cargo.toml
  $banquo_root/banquo-parser/Cargo.toml

Use --optifuzz-root to point at an existing checkout, or rerun after removing the incomplete directory.
EOF
    return 1
  fi

  echo "OptiFuzz Banquo dependency is ready: $banquo_root"
}

fastdyn_setup_main() {
  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  local venv_path="fastdyn-env"
  local python_bin="python3"
  local qemu_root="$repo_root/../qemu"
  local qemu_repo="${FASTDYN_QEMU_REPO:-https://github.com/Arslan8/qemu.git}"
  local qemu_ref="${FASTDYN_QEMU_REF:-fastdyn}"
  local libhw_root="$repo_root/../libhw"
  local optifuzz_root="$repo_root/../banquo"
  local optifuzz_repo="${FASTDYN_OPTIFUZZ_BANQUO_REPO:-https://github.com/michaelprooney/banquo.git}"
  local optifuzz_ref="${FASTDYN_OPTIFUZZ_BANQUO_REF:-banquo-parser-impl}"
  local update_submodules="true"
  local build_rumoca="false"
  local setup_optifuzz="true"
  local build_qemu="false"
  local build_gazebo="false"
  local setup_qemu_workspace="true"
  local upgrade_pip="true"

  while (($#)); do
    case "$1" in
      --venv)
        venv_path="${2:?missing value for --venv}"
        shift 2
        ;;
      --python)
        python_bin="${2:?missing value for --python}"
        shift 2
        ;;
      --skip-submodules)
        update_submodules="false"
        shift
        ;;
      --with-rumoca)
        build_rumoca="true"
        shift
        ;;
      --skip-rumoca)
        build_rumoca="false"
        shift
        ;;
      --build-qemu)
        build_qemu="true"
        shift
        ;;
      --build-gazebo)
        build_gazebo="true"
        shift
        ;;
      --qemu-root)
        qemu_root="${2:?missing value for --qemu-root}"
        shift 2
        ;;
      --qemu-repo)
        qemu_repo="${2:?missing value for --qemu-repo}"
        shift 2
        ;;
      --qemu-ref)
        qemu_ref="${2:?missing value for --qemu-ref}"
        shift 2
        ;;
      --libhw-root)
        libhw_root="${2:?missing value for --libhw-root}"
        shift 2
        ;;
      --optifuzz-root)
        optifuzz_root="${2:?missing value for --optifuzz-root}"
        shift 2
        ;;
      --optifuzz-repo)
        optifuzz_repo="${2:?missing value for --optifuzz-repo}"
        shift 2
        ;;
      --optifuzz-ref)
        optifuzz_ref="${2:?missing value for --optifuzz-ref}"
        shift 2
        ;;
      --skip-optifuzz)
        setup_optifuzz="false"
        shift
        ;;
      --skip-qemu-workspace)
        setup_qemu_workspace="false"
        shift
        ;;
      --no-upgrade-pip)
        upgrade_pip="false"
        shift
        ;;
      -h|--help)
        fastdyn_setup_usage
        return 0
        ;;
      *)
        echo "unknown option: $1" >&2
        fastdyn_setup_usage >&2
        return 2
        ;;
    esac
  done

  if [[ "$venv_path" != /* ]]; then
    venv_path="$repo_root/$venv_path"
  fi
  if [[ "$qemu_root" != /* ]]; then
    qemu_root="$repo_root/$qemu_root"
  fi
  if [[ "$libhw_root" != /* ]]; then
    libhw_root="$repo_root/$libhw_root"
  fi
  if [[ "$optifuzz_root" != /* ]]; then
    optifuzz_root="$repo_root/$optifuzz_root"
  fi

  if ! command -v "$python_bin" >/dev/null 2>&1; then
    echo "$python_bin was not found. Install Python 3 with venv support." >&2
    return 1
  fi

  if ! command -v ctags >/dev/null 2>&1; then
    echo "ctags was not found. Install universal-ctags or exuberant-ctags (e.g., sudo apt-get install universal-ctags)." >&2
    return 1
  fi

  "$python_bin" -m venv "$venv_path" || return

  local venv_python="$venv_path/bin/python"
  if [[ ! -x "$venv_python" ]]; then
    echo "virtual environment did not create $venv_python" >&2
    return 1
  fi

  if [[ "$upgrade_pip" == "true" ]]; then
    "$venv_python" -m pip install --upgrade pip || return
  fi

  "$venv_python" -m pip install -r "$repo_root/requirements.txt" || return
  "$venv_python" -m pip install -e "$repo_root/src" || return
  export PATH="$venv_path/bin:$PATH"

  if [[ "$update_submodules" == "true" ]]; then
    local submodules=(
      third_party/common/cmsis-svd-data
      third_party/courbet_deps/mavlink_headers
    )
    if [[ "$build_gazebo" == "true" ]]; then
      submodules+=(
        third_party/courbet_deps/SITL_Models
        third_party/courbet_deps/ardupilot_gazebo
      )
    fi
    if [[ "$build_rumoca" == "true" ]]; then
      submodules+=(
        third_party/common/rumoca
        third_party/common/modelica_models
      )
    fi
    git -C "$repo_root" submodule update --init --depth 1 --jobs 4 "${submodules[@]}" || return
  fi

  if [[ "$build_qemu" == "true" ]]; then
    fastdyn_setup_qemu_build "$repo_root" "$qemu_root" "$qemu_repo" "$qemu_ref" || return
    if [[ "$build_gazebo" != "true" ]]; then
      fastdyn_setup_plugin_build "$repo_root" "$qemu_root" || return
    fi
  fi
  if [[ "$build_gazebo" == "true" ]]; then
    fastdyn_setup_gazebo_plugin_build "$repo_root" "$qemu_root" "$libhw_root" || return
  fi
  if [[ "$setup_qemu_workspace" == "true" ]]; then
    fastdyn_setup_qemu_workspace "$qemu_root" || return
  fi

  if [[ "$build_rumoca" == "true" ]]; then
    if ! command -v cargo >/dev/null 2>&1; then
      echo "cargo was not found. Install Rust/Cargo or rerun with --skip-rumoca." >&2
      return 1
    fi
    if [[ ! -f "$repo_root/third_party/common/rumoca/Cargo.toml" ]]; then
      echo "Rumoca submodule is missing. Rerun without --skip-submodules, or use --skip-rumoca." >&2
      return 1
    fi
    (cd "$repo_root/third_party/common/rumoca" && cargo build --locked -p rumoca) || return
  fi
  if [[ "$setup_optifuzz" == "true" ]]; then
    fastdyn_setup_optifuzz_deps "$optifuzz_root" "$optifuzz_repo" "$optifuzz_ref" || return
  fi

  export FASTDYN_VENV="$venv_path"
  fastdyn_setup_did_setup="true"
  echo "FastDyn Python environment is ready: $FASTDYN_VENV"
  if [[ "$setup_qemu_workspace" == "true" ]]; then
    fastdyn_setup_qemu_hint "$repo_root" "$qemu_root" "$qemu_repo" "$qemu_ref"
    fastdyn_setup_plugin_hint "$repo_root" "$qemu_root"
  fi
  if [[ "$build_gazebo" == "true" ]]; then
    echo "Legacy Gazebo wrapper is ready: $repo_root/virtuals/physics/physics_engines/gazebo/build/lib/libgz_wrapper.so"
    echo "Courbet Gazebo services are ready: $repo_root/virtuals/physics/flight_controllers/courbet/gazebo/build/services"
    echo "libhw is ready: $libhw_root/out/libhw.so"
  fi
  if [[ "$build_rumoca" == "true" ]]; then
    echo "Run the Courbet ArduCopter FMI v3 mission with MAVCesium: fastdyn run -c configs/copter462.toml"
    echo "MAVCesium web viewer will be available at: http://127.0.0.1:5000/mavcesium/"
    echo "Run a 20-worker isolated local swarm: fastdyn swarm -c configs/copter462.toml -n 20 -o out/swarm/copter"
    echo "Run an OptiFuzz FMUv3 campaign: cd virtuals/fuzzer/libafl_phi && cargo run --bin baby_fuzzer"
    echo "Timing/profiling output will be under: fastdyn_work/fastdyn_timing.jsonl and fastdyn_work/profiles/"
    echo "Optional standalone Rumoca viewer is configured in [Rumoca] and can be enabled in configs/copter462.toml."
  fi
}

fastdyn_setup_did_setup="false"

fastdyn_setup_main "$@"
fastdyn_setup_status=$?

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  if [[ $fastdyn_setup_status -eq 0 && "$fastdyn_setup_did_setup" == "true" ]]; then
    # shellcheck disable=SC1091
    source "$FASTDYN_VENV/bin/activate"
  fi
  return "$fastdyn_setup_status"
fi

if [[ $fastdyn_setup_status -eq 0 && "$fastdyn_setup_did_setup" == "true" ]]; then
  echo "Activate it with: source \"$FASTDYN_VENV/bin/activate\""
fi
exit "$fastdyn_setup_status"
