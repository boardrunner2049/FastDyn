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
  # The pinned Rumoca enables FMU packaging and the viewer by default;
  # its former "lockstep" Cargo feature no longer exists.
  (cd "$repo_root/third_party/common/rumoca" && cargo build -p rumoca --release) || return
}
