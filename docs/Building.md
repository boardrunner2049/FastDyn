# Building FastDyn

The README has the short path (`source ./setup.sh --build-qemu --build-libhw`).
This page has the details: what `setup.sh` does and its options, building by
hand, the `make` flags, and the optional parts (LibVIO, installing for model
builds elsewhere, SUNDIALS, Gazebo, Rumoca).

## Dependencies

FastDyn builds against dependencies pinned as submodules:

- `third_party/common/qemu`: the patched QEMU fork that hosts the FastDyn
  plugin (FastDyn's `patches/qemu-fastdyn-plugin-icount.patch` is applied on
  top)
- `third_party/common/libhw`: the hardware-probe library (ST-Link / OpenOCD /
  J-Link), needed only to talk to a board (passthrough, twintrace recording)
- `third_party/common/libvio` (optional): the library device models use for
  buses, DMA, signals and host I/O; `fastdyn build-model` links models against it
- `third_party/common/cmsis-svd-data`: SVD files for `-b <board>`

System packages (Ubuntu 24.04):

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git meson ninja-build pkg-config python3-venv \
  libglib2.0-dev libcjson-dev libpixman-1-dev libfdt-dev zlib1g-dev flex bison \
  libstlink-dev libusb-1.0-0-dev
```

`libstlink-dev` and `libusb-1.0-0-dev` are for libhw only. Rust (`cargo`) is
needed only for Rumoca, OptiFuzz and the libFuzz fuzzer library.

## setup.sh

`source ./setup.sh [OPTIONS]` creates the virtual environment `fastdyn-env/`
with the `fastdyn` command, checks out the default submodules (SVD data, the
Courbet assets), and creates the QEMU RAM backing files. Build options add:

| Option | Builds |
| --- | --- |
| `--clone-qemu` | Download missing QEMU sources without patching or compiling; preserve existing checkouts |
| `--build-qemu` | The patched QEMU, then FastDyn (`build/libfastdyn.so`) with device models (`DEV`), the physics engine with the FMU and flight-controller backends, and libhw (`LIBHW`) when libhw is built |
| `--build-libhw` | libhw (`out/libhw.so` in the libhw checkout) |
| `--build-libvio` | LibVIO into `out/libvio/install`, where `fastdyn build-model` looks first |
| `--build-gazebo` | The legacy Gazebo/Courbet backend and FastDyn with `LIBGZ`/`LIBHW` (needs Gazebo Harmonic) |
| `--with-rumoca` | The pinned Rumoca toolchain and Modelica models, for FMU generation |

Sourcing it activates the environment; in a new shell, run
`source fastdyn-env/bin/activate`. Re-running it is safe: finished builds are
reused; QEMU runs an incremental build so local source edits are included.
`source ./setup.sh --help` lists every option.

Which QEMU and libhw checkout setup uses, unless `--qemu-root` /
`--libhw-root` names one:

1. the pinned submodule, if it is already built, or checked out and being built;
2. otherwise an already-built `../qemu` / `../libhw` next to the FastDyn
   checkout (the layout before the submodules), so existing setups keep
   working without a rebuild; setup says when it uses one;
3. otherwise the pinned submodule, which the build option checks out.

A missing pinned QEMU submodule is initialized at its recorded commit by
`--clone-qemu` or `--build-qemu`. Existing QEMU source checkouts retain their
remote, branch and local changes; builds never fetch or switch refs.

For a new external `--qemu-root`, use `--clone-qemu` to download `--qemu-repo`
at `--qemu-ref` (default `https://github.com/boardrunner2049/qemu.git`,
`fastdyn`). `--build-qemu` on its own requires existing external sources:

```bash
# Download only; existing checkouts are left in place.
source ./setup.sh --clone-qemu --qemu-root ../qemu

# Patch and incrementally build those sources, then build FastDyn.
source ./setup.sh --build-qemu --qemu-root ../qemu
```

Combine both options for a first-time external checkout and build. Both
options also perform the normal Python/environment setup.

The QEMU RAM backing files go to `../qemu/ws/`, where the Courbet configs name
them, or to `<qemu-root>/ws/` with `--qemu-root`.

The steps live in `scripts/setup/` (one file per part: `python.sh`,
`qemu.sh`, `libhw.sh`, `libvio.sh`, `fastdyn.sh`, `gazebo.sh`, `rumoca.sh`,
`optifuzz.sh`, and `common.sh` for shared helpers); `setup.sh` holds the
options and the order of the steps.

## Building by hand

From the repository root:

```bash
git submodule update --init --depth 1 third_party/common/qemu third_party/common/libhw

# libhw → third_party/common/libhw/out/libhw.so
make -C third_party/common/libhw

# QEMU (fork) → qemu-system-arm + plugin headers
cd third_party/common/qemu
git apply ../../../patches/qemu-fastdyn-plugin-icount.patch
mkdir build && cd build
../configure --target-list=arm-softmmu --enable-plugins
make qemu-system-arm
cd ../../../..

# FastDyn → build/libfastdyn.so
make DEV=true LIBHW=true
```

`make` uses the submodules once they are built; `qemu_path=<path/to/qemu>` and
`libhw_path=<path/to/libhw>` point it at other checkouts.

## make flags

`make` reconfigures from scratch with the flags given, and every feature is
off unless named. After changing code, `ninja -C build` rebuilds with the
current configuration.

| Flag | Default | Enables |
| --- | --- | --- |
| `DEV` | `false` | Device models: classic, elder, twintrace (and passthrough with `LIBHW`) |
| `LIBHW` | `false` | Talking to a board via libhw: passthrough and twintrace recording (elder and twintrace replay work without it) |
| `LIBFUZZ` | `false` | LibAFL fuzzing harness (also builds `virtuals/fuzzer/fastdyn_fuzz_lib` with cargo) |
| `AFLNET` | `false` | AFLNet harness (`aflnet_path`, default `../aflnet`) |
| `DEBUG_PRINT` | `false` | Verbose plugin logging |
| `PHY` | `false` | Physics engine; turned on by `LIBGZ`, `FMU` or `FLIGHT_CONTROLLERS` |
| `FMU` | `false` | FMI v3 FMU physics |
| `FLIGHT_CONTROLLERS` | `false` | ArduPilot / PX4 SITL adapters |
| `LIBGZ` | `false` | Gazebo / SITL physics integration |
| `SUNDIALS` | `false` | SUNDIALS solver for FMU physics (`sudo apt-get install -y libsundials-dev`) |
| `LIBPY` | `false` | Halucinator-mode Python callbacks (currently does not build) |

What `setup.sh --build-qemu` builds is
`make DEV=true PHY=true FLIGHT_CONTROLLERS=true FMU=true`, plus `LIBHW=true`
when libhw is built.

## Finding libfastdyn.so at run time

`fastdyn run` finds `build/libfastdyn.so` in the checkout. Other programs that
load it need:

```bash
export LD_LIBRARY_PATH=$PWD/build${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
```

## LibVIO for device models

Device models that use LibVIO are built with it by `fastdyn build-model`:

```bash
source ./setup.sh --build-libvio
```

This checks out the `third_party/common/libvio` submodule and installs it into
`out/libvio/install`, where `build-model` looks first. An installed LibVIO
found by `pkg-config vio` works too.

## Installing for building device models elsewhere

`meson install` installs `libfastdyn.so`, the device model headers
(`<fastdyn/device_model.h>`, `<fastdyn/fuzz_hooks.h>`), QEMU's
`<qemu/qemu-plugin.h>` and `fastdyn.pc`, so device models can be compiled
outside this checkout with `pkg-config --cflags fastdyn` (add
`pkg-config --libs fastdyn` only for models that call the fuzzer hooks):

```bash
meson configure build -Dprefix=$HOME/.local
meson install -C build
export PKG_CONFIG_PATH=$HOME/.local/lib/x86_64-linux-gnu/pkgconfig:$PKG_CONFIG_PATH
```

## Docker

The `Dockerfile` builds FastDyn, its patched QEMU, libhw, the libFuzz fuzzer
and the Gazebo/Courbet flight-controller backend from this checkout; see the
README for the commands. It runs the same steps as `setup.sh` and `make`
explicitly, because the build context has no `.git`.
