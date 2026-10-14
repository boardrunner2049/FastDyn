# The Python virtual environment with the fastdyn command, and the default
# submodules. Sourced by setup.sh.

fastdyn_setup_python() {
  local repo_root="$1"
  local python_bin="$2"
  local venv_path="$3"
  local upgrade_pip="$4"

  if ! command -v "$python_bin" >/dev/null 2>&1; then
    echo "$python_bin was not found. Install Python 3 with venv support." >&2
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
}

# SVD data and the Courbet assets always; ardupilot_gazebo with --build-gazebo;
# Rumoca and the Modelica models with --with-rumoca. QEMU, libhw and LibVIO are
# checked out by their own build steps.
fastdyn_setup_submodules() {
  local repo_root="$1"
  local build_gazebo="$2"
  local build_rumoca="$3"

  local submodules=(
    third_party/common/cmsis-svd-data
    third_party/courbet_deps/SITL_Models
    third_party/courbet_deps/mavlink_headers
  )
  if [[ "$build_gazebo" == "true" ]]; then
    submodules+=(
      third_party/courbet_deps/ardupilot_gazebo
    )
  fi
  if [[ "$build_rumoca" == "true" ]]; then
    submodules+=(
      third_party/common/rumoca
      third_party/common/modelica_models
    )
  fi
  git -C "$repo_root" submodule update --init "${submodules[@]}" || return
}
