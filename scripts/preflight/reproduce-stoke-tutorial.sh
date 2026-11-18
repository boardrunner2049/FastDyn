#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository="$(cd -- "${script_dir}/../.." && pwd)"
source_dir="${repository}/third_party/preflight/stoke"
revision="98d8a0f028f2daf2052bfe607dbc32ec8d55ba9e"
image="stanfordpl/stoke@sha256:c50f06b36602b4282c0730da58c119d809edd69732923dd137860aaa931a599d"
container="fastdyn-stoke-reproduce-$$"

if [[ "$(git -C "${source_dir}" rev-parse HEAD)" != "${revision}" ]]; then
    printf 'STOKE checkout is not at %s\n' "${revision}" >&2
    exit 1
fi

cleanup() {
    docker rm -f "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --name "${container}" --detach --volume "${source_dir}:/source:ro" \
    "${image}" sleep infinity >/dev/null
docker exec --user stoke "${container}" bash -lc '
    cp -a /source /tmp/stoke
    cd /tmp/stoke
    git clean -ffdx
    git submodule update --init --recursive
    make -j1 external
    make -j4
    export PATH=/tmp/stoke/bin:$PATH
    cd examples/tutorial
    make clean
    make orig extract testcase
    stoke synthesize --config synthesize.conf --seed 1
    stoke debug verify --config verify.conf
    install -m 0755 a.out a.out.reference
    stoke replace --config replace.conf
    for value in 0 1 10 100 10000; do
        set +e
        ./a.out.reference "$value"
        reference=$?
        ./a.out "$value"
        rewritten=$?
        set -e
        test "$reference" -eq "$rewritten"
    done
'
