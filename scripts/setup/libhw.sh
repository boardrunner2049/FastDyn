# libhw, the hardware-probe library (ST-Link / OpenOCD / J-Link) passthrough
# and twintrace recording use. Sourced by setup.sh.

fastdyn_setup_libhw_build() {
  local repo_root="$1"
  local libhw_root="$2"
  local libhw_so="$libhw_root/out/libhw.so"

  if fastdyn_setup_same_dir "$libhw_root" "$repo_root/third_party/common/libhw"; then
    fastdyn_setup_submodule_init "$repo_root" third_party/common/libhw Makefile || return
  fi

  if [[ ! -f "$libhw_root/Makefile" ]]; then
    cat >&2 <<EOF
libhw checkout is missing or incomplete: $libhw_root
Initialize the pinned submodule with:
  git submodule update --init --depth 1 third_party/common/libhw
or use --libhw-root to point at an existing checkout.
EOF
    return 1
  fi

  make -C "$libhw_root" || return

  if [[ ! -f "$libhw_so" ]]; then
    echo "libhw build did not produce $libhw_so" >&2
    return 1
  fi
}
