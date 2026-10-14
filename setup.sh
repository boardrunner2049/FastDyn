#!/usr/bin/env bash
#
# FastDyn setup: options and the order of the steps. Each step lives in
# scripts/setup/<step>.sh.

fastdyn_setup_usage() {
  cat <<'USAGE'
Usage: source ./setup.sh [OPTIONS]
       ./setup.sh [OPTIONS]

Create/update the FastDyn Python environment and local runtime workspace.

Options:
  --venv PATH           Virtual environment path (default: fastdyn-env)
  --python PYTHON       Python executable used to create the venv (default: python3)
  --clone-qemu          Download missing QEMU sources without building; preserve existing checkouts
  --build-qemu          Build the patched QEMU and the FastDyn QEMU plugin (with device models,
                        the FMU/flight-controller physics, and libhw when it is built)
  --build-gazebo        Build legacy Gazebo/Courbet deps and FastDyn with LIBGZ/LIBHW
  --build-libhw         Build libhw (hardware-in-the-loop probe library) in the libhw root
  --build-libvio        Build the third_party/common/libvio submodule into out/libvio, where
                        fastdyn build-model finds it before an installed LibVIO
  --qemu-root PATH      Patched QEMU checkout/workspace root (default: the pinned
                        third_party/common/qemu submodule, checked out by --build-qemu; an
                        already-built ../qemu is used when the submodule is not checked out)
  --qemu-repo URL       Repository for --clone-qemu with a --qemu-root outside the submodule
                        (default: https://github.com/boardrunner2049/qemu.git)
  --qemu-ref REF        Ref/branch for --clone-qemu, likewise (default: fastdyn)
  --libhw-root PATH     libhw checkout root (default: the pinned third_party/common/libhw
                        submodule, checked out by --build-libhw/--build-gazebo; an already-built
                        ../libhw is used when the submodule is not checked out)
  --optifuzz-root PATH  Banquo checkout root for OptiFuzz (default: ../banquo)
  --optifuzz-repo URL   Banquo repository (default: https://github.com/michaelprooney/banquo.git)
  --optifuzz-ref REF    Banquo ref/branch (default: banquo-parser-impl)
  --with-rumoca         Initialize/build pinned Rumoca for FMU generation and lockstep viewing
  --skip-optifuzz       Do not create/update the Banquo dependency checkout for OptiFuzz
  --skip-rumoca         Do not initialize/build pinned Rumoca for FMU generation and lockstep viewing
  --skip-qemu-workspace Do not create the default QEMU RAM backing files
  --skip-submodules     Do not initialize the default Git submodules (SVD data, Courbet assets)
  --no-upgrade-pip      Do not upgrade pip before installing dependencies
  -h, --help            Show this help

When sourced, this script activates the virtual environment after setup.
When executed, it prints the activation command.
For a new external --qemu-root, use --clone-qemu --build-qemu together.
The default pinned QEMU submodule is initialized by either option.
USAGE
}

fastdyn_setup_main() {
  local repo_root="$1"
  shift

  local venv_path="fastdyn-env"
  local python_bin="python3"
  local qemu_root=""
  local qemu_repo="${FASTDYN_QEMU_REPO:-https://github.com/boardrunner2049/qemu.git}"
  local qemu_ref="${FASTDYN_QEMU_REF:-fastdyn}"
  local libhw_root=""
  local optifuzz_root="$repo_root/../banquo"
  local optifuzz_repo="${FASTDYN_OPTIFUZZ_BANQUO_REPO:-https://github.com/michaelprooney/banquo.git}"
  local optifuzz_ref="${FASTDYN_OPTIFUZZ_BANQUO_REF:-banquo-parser-impl}"
  local update_submodules="true"
  local build_rumoca="false"
  local setup_optifuzz="true"
  local clone_qemu="false"
  local build_qemu="false"
  local build_gazebo="false"
  local build_libhw="false"
  local build_libvio="false"
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
      --clone-qemu)
        clone_qemu="true"
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
      --build-libhw)
        build_libhw="true"
        shift
        ;;
      --build-libvio)
        build_libvio="true"
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

  # The QEMU RAM backing files go where the Courbet configs name them
  # (../qemu/ws), or under an explicit --qemu-root as before.
  local qemu_ws="$repo_root/../qemu/ws"
  if [[ -n "$qemu_root" ]]; then
    qemu_ws="$qemu_root/ws"
  else
    qemu_root="$(fastdyn_setup_pick_root "$repo_root/third_party/common/qemu" "$repo_root/../qemu" \
      build/qemu-system-arm configure "$build_qemu")"
    if [[ "$qemu_root" == "$repo_root/../qemu" ]]; then
      echo "Using the already-built QEMU in $qemu_root (third_party/common/qemu is not checked out)"
    fi
  fi
  if [[ -z "$libhw_root" ]]; then
    local building_libhw="false"
    if [[ "$build_libhw" == "true" || "$build_gazebo" == "true" ]]; then
      building_libhw="true"
    fi
    libhw_root="$(fastdyn_setup_pick_root "$repo_root/third_party/common/libhw" "$repo_root/../libhw" \
      out/libhw.so Makefile "$building_libhw")"
    if [[ "$libhw_root" == "$repo_root/../libhw" ]]; then
      echo "Using the already-built libhw in $libhw_root (third_party/common/libhw is not checked out)"
    fi
  fi

  # Relative paths are relative to the checkout. (Written out per variable:
  # setup.sh is also sourced from zsh, which has no ${!name} or printf -v.)
  if [[ "$venv_path" != /* ]]; then
    venv_path="$repo_root/$venv_path"
  fi
  if [[ "$qemu_root" != /* ]]; then
    qemu_root="$repo_root/$qemu_root"
  fi
  if [[ "$qemu_ws" != /* ]]; then
    qemu_ws="$repo_root/$qemu_ws"
  fi
  if [[ "$libhw_root" != /* ]]; then
    libhw_root="$repo_root/$libhw_root"
  fi
  if [[ "$optifuzz_root" != /* ]]; then
    optifuzz_root="$repo_root/$optifuzz_root"
  fi

  if [[ "$build_qemu" == "true" && "$clone_qemu" != "true" ]] &&
     ! fastdyn_setup_same_dir "$qemu_root" "$repo_root/third_party/common/qemu"; then
    fastdyn_setup_qemu_require_source "$repo_root" "$qemu_root" || return
  fi

  fastdyn_setup_python "$repo_root" "$python_bin" "$venv_path" "$upgrade_pip" || return

  if [[ "$update_submodules" == "true" ]]; then
    fastdyn_setup_submodules "$repo_root" "$build_gazebo" "$build_rumoca" || return
  fi

  if [[ "$build_libhw" == "true" ]]; then
    fastdyn_setup_libhw_build "$repo_root" "$libhw_root" || return
    echo "libhw is ready: $libhw_root/out/libhw.so"
  fi
  if [[ "$build_libvio" == "true" ]]; then
    fastdyn_setup_libvio_build "$repo_root" || return
    echo "LibVIO is ready: $repo_root/out/libvio/install (fastdyn build-model uses it)"
  fi
  if [[ "$clone_qemu" == "true" ]]; then
    if fastdyn_setup_same_dir "$qemu_root" "$repo_root/third_party/common/qemu"; then
      fastdyn_setup_submodule_init "$repo_root" third_party/common/qemu configure || return
    else
      fastdyn_setup_qemu_clone "$qemu_root" "$qemu_repo" "$qemu_ref" || return
    fi
  fi
  if [[ "$build_qemu" == "true" ]]; then
    fastdyn_setup_qemu_build "$repo_root" "$qemu_root" || return
    if [[ "$build_gazebo" != "true" ]]; then
      fastdyn_setup_plugin_build "$repo_root" "$qemu_root" "$libhw_root" || return
    fi
  fi
  if [[ "$build_gazebo" == "true" ]]; then
    fastdyn_setup_gazebo_plugin_build "$repo_root" "$qemu_root" "$libhw_root" || return
  fi
  if [[ "$setup_qemu_workspace" == "true" ]]; then
    fastdyn_setup_qemu_workspace "$qemu_ws" || return
  fi

  if [[ "$build_rumoca" == "true" ]]; then
    fastdyn_setup_rumoca_build "$repo_root" || return
  fi
  if [[ "$setup_optifuzz" == "true" ]]; then
    fastdyn_setup_optifuzz_deps "$optifuzz_root" "$optifuzz_repo" "$optifuzz_ref" || return
  fi

  export FASTDYN_VENV="$venv_path"
  fastdyn_setup_did_setup="true"
  echo "FastDyn Python environment is ready: $FASTDYN_VENV"
  if [[ "$setup_qemu_workspace" == "true" ]]; then
    fastdyn_setup_qemu_hint "$repo_root" "$qemu_root" "$qemu_repo" "$qemu_ref"
    fastdyn_setup_plugin_hint "$repo_root" "$qemu_root" "$libhw_root"
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
fastdyn_setup_status=0
fastdyn_setup_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for fastdyn_setup_module in common python qemu libhw libvio fastdyn gazebo rumoca optifuzz; do
  # shellcheck disable=SC1090
  if ! source "$fastdyn_setup_repo_root/scripts/setup/$fastdyn_setup_module.sh"; then
    echo "cannot load $fastdyn_setup_repo_root/scripts/setup/$fastdyn_setup_module.sh" >&2
    fastdyn_setup_status=1
    break
  fi
done
unset fastdyn_setup_module

if [[ $fastdyn_setup_status -eq 0 ]]; then
  fastdyn_setup_main "$fastdyn_setup_repo_root" "$@"
  fastdyn_setup_status=$?
fi

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
