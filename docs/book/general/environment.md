# Choose your environment

Choose **one** setup below. Nix, Docker, and a manual installation provide the
same FastDyn commands. The tutorial then uses `fastdyn-config`, `fastdyn`, and
`python`; simulation choices belong in TOML files.

The supplied simulation environment targets **x86-64 Linux**. Prepare it before
the one-hour walkthrough: the first compiler and QEMU builds can take much longer
than an exercise. Run all tutorial commands from the FastDyn repository root.

## Get the sources

Clone the repository's `main` branch:

```bash
git clone --branch main https://github.com/jgoppert/FastDyn.git
cd FastDyn
```

If you already have this checkout, use it. From the repository root, initialize
the pinned compiler, model library, and peripheral descriptions:

```bash
git submodule update --init --depth 1 \
  third_party/common/rumoca \
  third_party/common/modelica_models \
  third_party/common/cmsis-svd-data
```

The source revisions are recorded in Git and `flake.lock`. Ordinary output goes
under the ignored `out/` directory.

## Option A: Nix

Follow the [official Nix installation guide](https://nixos.org/download/).
Nix is a package manager; it does not require installing NixOS. Read the
[flake introduction](https://nix.dev/concepts/flakes.html) for how `flake.nix`
and `flake.lock` describe and pin the environment.

Enable `nix-command` and `flakes` as described in the
[Nix configuration reference](https://nix.dev/manual/nix/stable/command-ref/conf-file.html#conf-experimental-features).
For a standard Linux installation, add this to `~/.config/nix/nix.conf`,
preserving existing settings:

```text
extra-experimental-features = nix-command flakes
```

On NixOS, use `nix.settings.experimental-features = [ "nix-command" "flakes" ];`
in the system configuration instead. Skip this if your installer enabled both.

```bash
nix --version
nix flake metadata --no-write-lock-file
nix develop
```

Expect the flake description **FastDyn development environment and
documentation**, then a shell with Python, the patched QEMU, FastDyn's plugin,
Rumoca, build tools, and mdBook. Later sessions reuse built dependencies.
Exit this shell with `exit`.

## Option B: Docker

Install [Docker Engine](https://docs.docker.com/engine/install/). Use the
published image from the repository containing this book. For `jgoppert/FastDyn`:

```bash
docker pull ghcr.io/jgoppert/fastdyn/dev:latest
docker run --rm -it --user "$(id -u):$(id -g)" \
  --volume "$PWD:/workspace" --publish 5000:5000 --publish 3000:3000 \
  ghcr.io/jgoppert/fastdyn/dev:latest
```

You are now at `/workspace`, with the checkout mounted and tools available.
Files created under `out/` belong to your host user. Port 5000 exposes the live
mission viewer; port 3000 is for an optional documentation server. Run the
simulation and its helpers together inside the container.

If you have a locally built image archive instead, load it and start the same
environment:

```bash
docker load --input fastdyn-dev.tar.gz
docker run --rm -it --user "$(id -u):$(id -g)" \
  --volume "$PWD:/workspace" --publish 5000:5000 --publish 3000:3000 \
  fastdyn-dev:local
```

You need only Docker to load and use that archive. See
[build and share the Docker environment](container.md) for the Nix build,
archive distribution, and container commands. In another repository, the
image address is `ghcr.io/<owner>/<repository>/dev:latest`, all lowercase.

## Option C: install and build the dependencies

The repository's native setup supports Ubuntu 24.04. Install its build tools:

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake device-tree-compiler git libexpat1-dev libfdt-dev \
  libglib2.0-dev libpixman-1-dev libudev-dev lsof meson ninja-build pkg-config \
  python3-dev python3-venv universal-ctags zlib1g-dev
```

Install Rust using [rustup's instructions](https://rust-lang.org/tools/install/)
if `cargo` is not already available. Then build and enter the Python environment:

```bash
source ./setup.sh --build-qemu --with-rumoca --skip-optifuzz
```

This builds the patched QEMU and plugin, installs Python dependencies and the
`fastdyn` / `fastdyn-config` commands, and builds the pinned native Rumoca.
For later terminals, run `source fastdyn-env/bin/activate`.

Nix, Docker, and the native setup use the same pinned Rumoca compiler. The
repository's Git submodule and `flake.lock` record its exact revision.

## Check the common commands

Inside whichever environment you chose:

```bash
fastdyn run --help
fastdyn-config --help
rumoca --version
python -c "import fastdyn, fmpy, pymavlink; print('Python dependencies available')"
```

Expect command help, `rumoca 0.10.0`, and `Python dependencies available`.
Continue to [architecture and first steps](overview.md) or the
[first Rumoca mission](../rumoca/getting-started.md).
