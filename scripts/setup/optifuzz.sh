# The Banquo checkout OptiFuzz (virtuals/fuzzer/libafl_phi) builds against.
# Sourced by setup.sh.

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
