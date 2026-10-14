# The pinned Rumoca toolchain, for FMU generation and lockstep viewing.
# Sourced by setup.sh.

fastdyn_setup_rumoca_build() {
  local repo_root="$1"

  if ! command -v cargo >/dev/null 2>&1; then
    echo "cargo was not found. Install Rust/Cargo or rerun with --skip-rumoca." >&2
    return 1
  fi
  if [[ ! -f "$repo_root/third_party/common/rumoca/Cargo.toml" ]]; then
    echo "Rumoca submodule is missing. Rerun without --skip-submodules, or use --skip-rumoca." >&2
    return 1
  fi
  (cd "$repo_root/third_party/common/rumoca" && cargo build -p rumoca --features lockstep --release) || return
}
