#!/usr/bin/env bash
# One-time container setup. Re-runnable — skips what's already there.
# Both containers live as sleep-infinity daemons so we can `docker exec`
# each benchmark run without re-paying container startup cost.
#
# The HALucinator-FD (halucinator-fd) image ships standalone (source + compiled libfastdyn.so
# + QEMU baked in), so no host bind mount is needed. Set FASTDYN_MOUNT_SRC
# to a local Halucinator_Turbo checkout only if you want to run against
# locally-modified source instead of the shipped image.

set -euo pipefail

# Pinned artifact versions used to reproduce the paper's macrobench
# numbers. Override via env if you need a different tag/mount.
HALUCINATOR_IMAGE="${HALUCINATOR_IMAGE:-halucinator}"
FASTDYN_IMAGE="${FASTDYN_IMAGE:-pseclab/halucinator-fd@sha256:2ce475b5d5e7e1830767c0d9f352c5368e03b7c0063a6eafd6a1a5a9a8d772c2}"
FASTDYN_MOUNT_SRC="${FASTDYN_MOUNT_SRC:-}"   # empty = use image's baked-in source

need() {
    local name="$1"
    # `docker container inspect` matches only containers (not images);
    # the plain `docker inspect` would also match a same-named image and
    # confuse the "container exists" check.
    if docker container inspect "$name" >/dev/null 2>&1; then
        if ! docker ps --format '{{.Names}}' | grep -qx "$name"; then
            echo "starting existing container $name"
            docker start "$name" >/dev/null
        else
            echo "$name already running"
        fi
        return 0
    fi
    return 1
}

if ! need halucinator; then
    echo "creating halucinator from $HALUCINATOR_IMAGE"
    docker run -d --name halucinator --restart=unless-stopped "$HALUCINATOR_IMAGE" \
        sleep infinity >/dev/null
fi

if ! need halucinator-fd; then
    echo "creating halucinator-fd from $FASTDYN_IMAGE"
    if [[ -n "$FASTDYN_MOUNT_SRC" ]]; then
        docker run -d --name halucinator-fd --restart=unless-stopped \
            -v "${FASTDYN_MOUNT_SRC}:/root/halucinator" \
            "$FASTDYN_IMAGE" sleep infinity >/dev/null
    else
        docker run -d --name halucinator-fd --restart=unless-stopped \
            "$FASTDYN_IMAGE" sleep infinity >/dev/null
    fi
fi

# Sanity: pyzmq must be importable from /root/halucinator's Python.
for c in halucinator halucinator-fd; do
    if ! docker exec "$c" python3 -c 'import zmq' 2>/dev/null; then
        echo "WARN: $c missing pyzmq — attempting install"
        docker exec "$c" bash -lc 'pip install pyzmq' || {
            echo "ERROR: could not install pyzmq in $c"
            exit 1
        }
    fi
done

echo "ready."
