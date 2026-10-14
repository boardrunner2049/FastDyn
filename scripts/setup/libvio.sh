# LibVIO, the library device models use for buses, DMA, signals and host I/O.
# Built from the third_party/common/libvio submodule into out/libvio/install,
# where `fastdyn build-model` looks before an installed LibVIO. Sourced by
# setup.sh.

fastdyn_setup_libvio_build() {
  local repo_root="$1"
  local source_dir="$repo_root/third_party/common/libvio"
  local build_dir="$repo_root/out/libvio/build"
  local prefix="$repo_root/out/libvio/install"

  fastdyn_setup_submodule_init "$repo_root" third_party/common/libvio CMakeLists.txt || return
  cmake -S "$source_dir" -B "$build_dir" -DCMAKE_BUILD_TYPE=Release -DVIO_BUILD_TESTS=OFF || return
  cmake --build "$build_dir" -j"$(fastdyn_setup_build_jobs)" || return
  cmake --install "$build_dir" --prefix "$prefix" || return
}
