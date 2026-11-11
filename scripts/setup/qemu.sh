# The patched QEMU FastDyn runs in, and the QEMU RAM backing files.
# Sourced by setup.sh.

fastdyn_setup_qemu_patch() {
  local repo_root="$1"
  local qemu_root="$2"
  local patch
  local patches=(
    "$repo_root/patches/qemu-fastdyn-plugin-icount.patch"
    "$repo_root/patches/qemu-fastdyn-insn-fragments.patch"
  )

  # The last patch may touch context introduced by an earlier patch.  If the
  # complete series is present, checking that tip first avoids misclassifying
  # the earlier patch as incompatible merely because its context advanced.
  if [[ -f "${patches[1]}" ]] &&
     git -C "$qemu_root" apply --reverse --check "${patches[1]}" >/dev/null 2>&1; then
    echo "FastDyn QEMU ordered patch series is already applied"
    return 0
  fi

  for patch in "${patches[@]}"; do
    if [[ ! -f "$patch" ]]; then
      echo "missing required QEMU patch: $patch" >&2
      return 1
    fi
    if git -C "$qemu_root" apply --reverse --check "$patch" >/dev/null 2>&1; then
      echo "FastDyn QEMU patch is already applied: $patch"
      continue
    fi
    if git -C "$qemu_root" apply --check "$patch" >/dev/null 2>&1; then
      git -C "$qemu_root" apply "$patch" || return
      echo "Applied FastDyn QEMU patch: $patch"
      continue
    fi
    echo "FastDyn QEMU patch does not apply cleanly: $patch" >&2
    echo "The ordered patch series requires the pinned QEMU base and all earlier patches." >&2
    return 1
  done
}

fastdyn_setup_qemu_source_exists() {
  [[ -f "$1/configure" && -f "$1/include/qemu/qemu-plugin.h" ]]
}

fastdyn_setup_qemu_require_source() {
  local repo_root="$1"
  local qemu_root="$2"

  if fastdyn_setup_qemu_source_exists "$qemu_root"; then
    return 0
  fi

  cat >&2 <<EOF
QEMU sources are missing or incomplete: $qemu_root
--build-qemu only builds existing sources; it does not download them.
Download and build with:
  source "$repo_root/setup.sh" --clone-qemu --build-qemu --qemu-root "$qemu_root"
Or use --qemu-root to select an existing QEMU source checkout.
EOF
  return 1
}

fastdyn_setup_qemu_clone() {
  local qemu_root="$1"
  local qemu_repo="$2"
  local qemu_ref="$3"

  if fastdyn_setup_qemu_source_exists "$qemu_root"; then
    echo "Using existing QEMU sources without fetching or switching refs: $qemu_root"
    return 0
  fi

  # Preserve existing repositories, including worktrees with a .git file.
  if [[ -e "$qemu_root/.git" ]] &&
     git -C "$qemu_root" rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "Existing QEMU checkout is incomplete: $qemu_root" >&2
    echo "Restore its missing source files or select another --qemu-root." >&2
    return 1
  fi

  # setup may already have created ws/. git checkout protects untracked files.
  mkdir -p "$qemu_root" || return
  git -C "$qemu_root" init || return
  local origin
  if origin=$(git -C "$qemu_root" remote get-url origin 2>/dev/null); then
    if [[ "$origin" != "$qemu_repo" ]]; then
      echo "Incomplete QEMU repository has a different origin: $origin" >&2
      echo "Use --qemu-repo to match it or select another --qemu-root." >&2
      return 1
    fi
  else
    git -C "$qemu_root" remote add origin "$qemu_repo" || return
  fi
  git -C "$qemu_root" fetch --depth 1 origin "$qemu_ref" || return
  git -C "$qemu_root" checkout -b fastdyn FETCH_HEAD || return
  if ! fastdyn_setup_qemu_source_exists "$qemu_root"; then
    echo "Downloaded ref does not contain the expected QEMU sources: $qemu_ref" >&2
    return 1
  fi
  echo "QEMU sources are ready: $qemu_root (not built)"
}

fastdyn_setup_qemu_workspace() {
  local qemu_ws="$1"

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
  local qemu_patch="$repo_root/patches/qemu-fastdyn-insn-fragments.patch"

  if [[ -x "$qemu_bin" ]]; then
    if fastdyn_setup_qemu_source_exists "$qemu_root" &&
       ! git -C "$qemu_root" apply --reverse --check "$qemu_patch" >/dev/null 2>&1; then
      cat >&2 <<EOF
FastDyn patched QEMU exists, but the required ordered feature patch series
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

  if fastdyn_setup_same_dir "$qemu_root" "$repo_root/third_party/common/qemu"; then
    cat >&2 <<EOF
FastDyn patched QEMU is missing: $qemu_bin
Build the pinned third_party/common/qemu submodule with:
  source "$repo_root/setup.sh" --build-qemu
Use --clone-qemu alone to check out its sources without building.
EOF
    return 0
  fi

  if ! fastdyn_setup_qemu_source_exists "$qemu_root"; then
    cat >&2 <<EOF
QEMU sources are missing: $qemu_root
Download sources only with:
  source "$repo_root/setup.sh" --clone-qemu --qemu-root "$qemu_root" --qemu-repo "$qemu_repo" --qemu-ref "$qemu_ref"
To download and build in one run, also add --build-qemu.
EOF
    return 0
  fi

  cat >&2 <<EOF
FastDyn patched QEMU is missing: $qemu_bin
Build the existing sources and FastDyn plugin with:
  source "$repo_root/setup.sh" --build-qemu --qemu-root "$qemu_root"
EOF
}

fastdyn_setup_qemu_build() {
  local repo_root="$1"
  local qemu_root="$2"
  local targets="${3:-arm-softmmu}"
  local build_targets=()
  local configured_targets=""
  local need_configure="false"
  local target

  # Keep main's fresh-clone path: initialize the pinned submodule only when
  # its sources are missing. Existing checkouts are never fetched or reset.
  if fastdyn_setup_same_dir "$qemu_root" "$repo_root/third_party/common/qemu"; then
    fastdyn_setup_submodule_init "$repo_root" third_party/common/qemu configure || return
  fi
  fastdyn_setup_qemu_require_source "$repo_root" "$qemu_root" || return
  fastdyn_setup_qemu_patch "$repo_root" "$qemu_root" || return
  mkdir -p "$qemu_root/build" || return
  if [[ ! -f "$qemu_root/build/build.ninja" ]]; then
    need_configure="true"
  elif [[ -f "$qemu_root/build/config-host.mak" ]]; then
    configured_targets=$(sed -n 's/^TARGET_DIRS=//p' "$qemu_root/build/config-host.mak")
    IFS=',' read -ra build_targets <<< "$targets"
    for target in "${build_targets[@]}"; do
      if [[ " $configured_targets " != *" $target "* ]]; then
        need_configure="true"
        break
      fi
    done
  fi
  if [[ "$need_configure" == "true" ]]; then
    (cd "$qemu_root/build" && ../configure --target-list="$targets" --enable-plugins) || return
  fi
  IFS=',' read -ra build_targets <<< "$targets"
  for target in "${!build_targets[@]}"; do
    build_targets[$target]="qemu-system-${build_targets[$target]%-softmmu}"
  done
  make -C "$qemu_root/build" -j"$(fastdyn_setup_build_jobs)" "${build_targets[@]}" || return
}
