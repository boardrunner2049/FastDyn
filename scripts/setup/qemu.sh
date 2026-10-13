# The patched QEMU FastDyn runs in, and the QEMU RAM backing files. Sourced by
# setup.sh.

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
  local qemu_patch="$repo_root/patches/qemu-fastdyn-plugin-icount.patch"

  if [[ -x "$qemu_bin" ]]; then
    if [[ -e "$qemu_root/.git" ]] &&
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

  if fastdyn_setup_same_dir "$qemu_root" "$repo_root/third_party/common/qemu"; then
    cat >&2 <<EOF
FastDyn patched QEMU is missing: $qemu_bin
Build it (the pinned third_party/common/qemu submodule) with:
  source "$repo_root/setup.sh" --build-qemu
EOF
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

  if [[ -x "$qemu_bin" && -e "$qemu_root/.git" ]]; then
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

  if fastdyn_setup_same_dir "$qemu_root" "$repo_root/third_party/common/qemu"; then
    # The pinned submodule is built at exactly the recorded commit; it is
    # never re-fetched or moved to the branch tip.
    fastdyn_setup_submodule_init "$repo_root" third_party/common/qemu configure || return
  else
    mkdir -p "$qemu_root" || return
    git -C "$qemu_root" init || return
    git -C "$qemu_root" remote add origin "$qemu_repo" 2>/dev/null \
      || git -C "$qemu_root" remote set-url origin "$qemu_repo" \
      || return
    git -C "$qemu_root" fetch origin "$qemu_ref" || return
    git -C "$qemu_root" checkout -B fastdyn FETCH_HEAD || return
  fi
  fastdyn_setup_qemu_patch "$repo_root" "$qemu_root"
  local patch_status=$?
  if [[ $patch_status -ne 0 && $patch_status -ne 2 ]]; then
    return "$patch_status"
  fi
  mkdir -p "$qemu_root/build" || return
  if [[ ! -f "$qemu_root/build/build.ninja" ]]; then
    (cd "$qemu_root/build" && ../configure --target-list=arm-softmmu --enable-plugins) || return
  fi
  make -C "$qemu_root/build" -j"$(fastdyn_setup_build_jobs)" qemu-system-arm || return
}
