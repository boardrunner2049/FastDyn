# Helpers shared by the setup modules. Sourced by setup.sh.

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

# True when two paths name the same existing directory.
fastdyn_setup_same_dir() {
  [[ -d "$1" && -d "$2" ]] || return 1
  [[ "$(cd "$1" && pwd -P)" == "$(cd "$2" && pwd -P)" ]]
}

# Check out a pinned submodule (shallow) unless its sources are already there.
# `marker` is a file the checkout always has; testing for it rather than for
# .git also works in copies without git metadata (e.g. Docker build contexts).
fastdyn_setup_submodule_init() {
  local repo_root="$1"
  local submodule="$2"
  local marker="$3"

  if [[ -f "$repo_root/$submodule/$marker" ]]; then
    return 0
  fi
  git -C "$repo_root" submodule update --init --depth 1 "$submodule" || return
}

# Pick the checkout a dependency (QEMU, libhw) is used from when no explicit
# root was given:
#   1. the pinned submodule, if it is already built;
#   2. the pinned submodule, if its sources are checked out and this run builds it;
#   3. an already-built sibling checkout (../qemu, ../libhw: the layout before
#      the submodules), so existing setups keep working without a rebuild;
#   4. otherwise the pinned submodule (checked out when it is built).
# Prints the chosen root.
fastdyn_setup_pick_root() {
  local submodule="$1"
  local sibling="$2"
  local built_file="$3"
  local source_file="$4"
  local building="$5"

  if [[ -e "$submodule/$built_file" ]]; then
    echo "$submodule"
  elif [[ -f "$submodule/$source_file" && "$building" == "true" ]]; then
    echo "$submodule"
  elif [[ -e "$sibling/$built_file" ]]; then
    echo "$sibling"
  else
    echo "$submodule"
  fi
}
