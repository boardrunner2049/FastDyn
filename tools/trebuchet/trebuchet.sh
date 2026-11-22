#!/usr/bin/env bash
# Build, select and measure original and optimized deployments for real applications.
# The historical single-workload commands are available under the fixture subcommand.
set -euo pipefail

T=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
F=$(cd "$T/../.." && pwd)
# Real campaigns use pinned registries and explicit operational protocols.
case ${1:-} in
    candidates|select|final|dataset|dataset_report)
        command=$1
        shift
        python=${TREBUCHET_PYTHON:-"$F/scopt-env/bin/python"}
        case $command in
            candidates) exec "$python" "$T/lib/candidates.py" "$@" ;;
            select) exec "$python" "$T/lib/joint_selection.py" "$@" ;;
            final) exec "$python" "$T/lib/final_evaluation.py" final "$@" ;;
            dataset_report) exec "$python" "$T/lib/final_evaluation.py" report "$@" ;;
            dataset) exec "$python" "$T/lib/real_dataset.py" "$@" ;;
        esac ;;
    fixture) shift ;;
    *) echo "usage: trebuchet.sh {candidates|select|final|dataset|dataset_report} ... (archived correctness runner: fixture STAGE)" >&2; exit 2 ;;
esac
: "${TREBUCHET_WORK:?set TREBUCHET_WORK to a work directory outside the checkout}"
FIRMBOLT_REPO=${FIRMBOLT_REPO:-"$F/third_party/firmbolt"}
FIRMBOLT_BOLT=${FIRMBOLT_BOLT:-"$FIRMBOLT_REPO/third_party/preflight/llvm-build/bin/llvm-bolt"}
DATASET="compute fwbench kernbench libbench algobench"          # the categories, in report order
WL=${TREBUCHET_WORKLOAD:-compute}
case " $DATASET " in *" $WL "*) ;; *) echo "unknown workload $WL (one of: $DATASET)" >&2; exit 2;; esac
ROOT=$(mkdir -p "$TREBUCHET_WORK" && cd "$TREBUCHET_WORK" && pwd)   # shared: FreeRTOS, inputs, scopt-data
W=$ROOT/$WL; mkdir -p "$W"                                       # everything of one workload
export TREBUCHET_ROOT=$ROOT TREBUCHET_WORK=$W TREBUCHET_WORKLOAD=$WL TREBUCHET_INPUTS=$ROOT/inputs TREBUCHET_FASTDYN=$F
export TREBUCHET_QEMU=${TREBUCHET_QEMU:-$F/third_party/common/qemu/build/qemu-system-arm}
export ARM_PREFIX=${ARM_PREFIX:-arm-none-eabi-}
export STOCK_QEMU=${STOCK_QEMU:-/usr/bin/qemu-system-arm}
PIN=${TREBUCHET_CPUS:+taskset -c $TREBUCHET_CPUS}      # CPUs for timed runs, e.g. 8-11
FREERTOS_COMMIT=48d4714c71da903b39baa4615b623525a0c0e2a3
DEMO=$ROOT/FreeRTOS/FreeRTOS/Demo/CORTEX_MPS2_QEMU_IAR_GCC
MARKERS=vComputeWindowStart,vComputeReport1,vComputeReport2,vComputeReport3,vComputeReport4,vComputeDone

# scopt: everything it stores is bound to this QEMU executable (tools/scopt/docs/qemu.md).
Q=$TREBUCHET_QEMU
QSHA=$(sha256sum "$Q" | cut -d' ' -f1)
export SCOPT_ENVLAYOUT_PATH=$ROOT/scopt-data/envlayouts SCOPT_HELPER_EFFECTS_PATH=$ROOT/scopt-data/helper_effects
export SCOPT_V6_EXES=$QSHA SCOPT_RUNTIME_QEMU=$Q SCOPT_RUNTIME_QEMU_SHA256=$QSHA
export FASTDYN_ROOT=$W/scopt SCOPT_RAM_ROOT=${SCOPT_RAM_ROOT:-/dev/shm/trebuchet-ram}
PY=$(dirname "$(command -v scopt)")/python             # the environment scopt is installed in
mkdir -p "$SCOPT_ENVLAYOUT_PATH" "$SCOPT_HELPER_EFFECTS_PATH" "$FASTDYN_ROOT" "$SCOPT_RAM_ROOT"

inputs() { local s n; for s in "$@"; do for n in 1 2 3; do printf -- '--inputs %s ' "$ROOT/inputs/$s/$WL/$s-$n.bin"; done; done; }

stage_firmware() {      # pinned FreeRTOS demo + the five applications + nine inputs per workload
    if [ ! -d "$ROOT/FreeRTOS/.git" ]; then
        mkdir -p "$ROOT/FreeRTOS"
        git -C "$ROOT/FreeRTOS" init -q
        git -C "$ROOT/FreeRTOS" remote add origin https://github.com/FreeRTOS/FreeRTOS.git
        git -C "$ROOT/FreeRTOS" fetch -q --depth 1 origin $FREERTOS_COMMIT
        git -C "$ROOT/FreeRTOS" checkout -q FETCH_HEAD
        git -C "$ROOT/FreeRTOS" submodule -q update --init --depth 1 FreeRTOS/Source FreeRTOS-Plus/Source/FreeRTOS-Plus-Trace
    fi
    cp "$T"/firmware/compute/{main_compute.c,main.c,FreeRTOSConfig.h} "$T"/firmware/workloads/*.[ch] "$DEMO/"
    cp "$T"/firmware/compute/gcc/{Makefile,mps2_m3.ld,startup_gcc.c} "$DEMO/build/gcc/"
    rm -rf "$ROOT/inputs"
    python3 "$T/firmware/compute/gen_inputs.py" --out "$ROOT/inputs" > "$ROOT/inputs.log"
    for s in train heldout final; do mv "$ROOT/inputs/$s/freertos-mps2-compute" "$ROOT/inputs/$s/compute"; done
    python3 "$T/firmware/workloads/gen_params.py" --out "$ROOT/inputs" >> "$ROOT/inputs.log"
    echo "inputs: $(ls "$ROOT"/inputs/*/*/*.bin | wc -l)"
}

stage_build() {         # -O0 image of one workload, linked with --emit-relocs so FirmBOLT can relocate code
    local number
    number=$(python3 -c "import sys; sys.path.insert(0, '$T/lib'); from result_schema import WORKLOADS; print(WORKLOADS['$WL'])")
    mkdir -p "$W/fw/O0" "$DEMO/build/gcc/output"
    (   # the build directory is shared by all workloads: one build at a time
        flock 9
        make -C "$DEMO/build/gcc" clean > /dev/null
        TREBUCHET_CFLAGS="-DFW_WORKLOAD=$number" make -C "$DEMO/build/gcc" -j"$(nproc)" \
            CC="$T/lib/ccopt.py O0" LD="$T/lib/ccopt.py O0" > "$W/fw/O0/build.log" 2>&1
        cp "$DEMO/build/gcc/output/RTOSDemo.out" "$W/fw/O0/RTOSDemo.out"
    ) 9> "$ROOT/FreeRTOS/.trebuchet-build.lock"
    echo "$WL: $("${ARM_PREFIX}size" "$W/fw/O0/RTOSDemo.out" | tail -1)"
}

stage_firmbolt() {      # block profile on the training inputs inside FastDyn's machine, then FirmBOLT
    local P=$W/fbprof ELF=$W/fw/O0/RTOSDemo.out tools=$FIRMBOLT_REPO/tools/emulator-profile stop n
    mkdir -p "$P" "$W/fw/O0-fb"
    cc -O2 -shared -fPIC -o "$P/libtbprof.so" "$tools/firmbolt_tbprof.c" \
        -I "$F/third_party/common/qemu/include/qemu" -I "$F/third_party/common/qemu/include" $(pkg-config --cflags glib-2.0)
    python3 "$T/lib/gen_toml.py" "$ELF" "$P/prof.toml" fw --no-cache > /dev/null
    sed -i "s|^qemu_path = .*|qemu_path = \"$T/lib/qemu-prof.sh\"|" "$P/prof.toml"
    stop=$("${ARM_PREFIX}nm" "$ELF" | awk '$3=="vComputeDone"{print "0x"$1}'); stop=$(printf '0x%x' $(( stop & ~1 )))
    : > "$P/run.preagg"
    for n in 1 2 3; do
        rm -rf "$P/run$n"; mkdir -p "$P/run$n"
        cp "$ROOT/inputs/train/$WL/train-$n.bin" "$P/run$n/input.bin"; truncate -s 1M "$P/run$n/input.bin"
        (cd "$P/run$n" && FBPROF_PLUGIN=$P/libtbprof.so FBPROF_OUT=$P/train-$n.tbprof FBPROF_STOP=$stop \
            setarch x86_64 -R timeout 600 "$F/fastdyn-env/bin/fastdyn" run -c "$P/prof.toml" -o out > run.log 2>&1) || true
        "$PY" "$tools/tbprof2bolt.py" "$P/train-$n.tbprof" "$P/train-$n.preagg" > /dev/null
        cat "$P/train-$n.preagg" >> "$P/run.preagg"
        "$PY" "$tools/tbprof_report.py" "$ELF" "$P/train-$n.tbprof" 0 | sed -n 1p
    done
    # The marker functions are the benchmark's hook addresses: keep them in place.
    FIRMBOLT_BOLT=$FIRMBOLT_BOLT PYTHONPATH=$FIRMBOLT_REPO/src python3 -m firmbolt --cpu cortex-m3 \
        --emulator-profile "$P/run.preagg" --new-code-address 0x10000 "$ELF" -o "$W/fw/O0-fb/RTOSDemo.out" \
        -- "--skip-funcs=$MARKERS" > "$W/fw/O0-fb/firmbolt.log" 2>&1
    grep -E "^BOLT-INFO: (inlined|tb-page|relocation mode kept)" "$W/fw/O0-fb/firmbolt.log"
}

stage_bind() {          # scopt's per-executable data for this QEMU build
    scopt envlayout produce --exe "$Q" --out "$SCOPT_ENVLAYOUT_PATH/$QSHA.json" | tail -1
    scopt helper-effects rebind --from-exe-sha 56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90 --exe "$Q" \
        || "$PY" "$T/lib/rebind_helper_effects.py" "$Q" "$F/third_party/common/qemu"
}

optimize() {            # optimize VARIANT PIPELINE [scopt optimize arguments]
    local V=$1 P=$2 D=$FASTDYN_ROOT/$1; shift 2
    scopt optimize "$(ls -d "$D"/store/snapshots/*)" --pipeline "$P" --profile "$D/prof/merged.profile.json" \
        --store "$D/out/$P" --qemu "$Q" --v6-exe "$QSHA" --runtime-identity "$D/runtime_identity.v2.json" \
        --prep "$D/prep" --elf "$W/fw/$V/RTOSDemo.out" --selftest "$D/hostsym_selftest.json" --json "$@" \
        > "$D/out/$P.json" 2> "$D/out/$P.err" && echo "$V $P: artifact" || echo "$V $P: rejected"
}

compose_one() {         # one cache with the store-forwarding fix and chaining: b3 on the re-imported sfb artifact
    local V=$1 D=$FASTDYN_ROOT/$1 SFB S2
    SFB=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['path'])" "$D/out/sfb.json")
    scopt import "$SFB/cache.sc" $(for f in "$SFB"/cache*.scm; do printf -- '--scm %s ' "$f"; done) \
        --manifest "$D/prep/cache/fw.sc.fastdyn.json" --bundle "$D/bundle.json" \
        --runtime-identity "$D/runtime_identity.json" --store "$D/out/sfb-reimport" --no-progress > "$D/out/sfb-reimport.log"
    S2=$(ls -d "$D"/out/sfb-reimport/snapshots/*)
    scopt optimize "$S2" --pipeline b3 --b3 --enable specialize-returns --enable chain-returns --enable chain-static \
        --profile "$D/prof/merged.profile.json" --store "$D/out/sfb-b3" --qemu "$Q" --v6-exe "$QSHA" \
        --runtime-identity "$D/runtime_identity.v2.json" --prep "$D/prep" --elf "$W/fw/$V/RTOSDemo.out" \
        --selftest "$D/hostsym_selftest.json" --warm-baseline "$(ls -d "$D"/store/snapshots/*)" --warm-stage "$SFB" \
        --json > "$D/out/sfb-b3.json" 2> "$D/out/sfb-b3.err" && echo "$V sfb+b3: artifact" || echo "$V sfb+b3: rejected"
}

stage_compose() { compose_one O0; compose_one O0-fb; }

prepare_one() {         # capture, import, profile and candidate caches for one image
    local V=$1 D=$FASTDYN_ROOT/$1 CFG=$W/cfg/$1/fw.toml SNAP WS
    mkdir -p "$W/cfg/$V" "$D/out"
    python3 "$T/lib/gen_toml.py" "$W/fw/$V/RTOSDemo.out" "$CFG" fw
    python3 "$T/lib/gen_toml.py" "$W/fw/$V/RTOSDemo.out" "$W/cfg/$V/nocache.toml" fw --no-cache > /dev/null
    # The cache is captured over every input, so that timed runs translate nothing.
    # Profiles use the training inputs only.
    scopt prepare -c "$CFG" --out "$D/prep" --qemu "$Q" $(inputs train heldout final) > "$D/prepare.log"
    echo "$V prepare: $(sed -n 1p "$D/prepare.log")"
    scopt bundle identity -c "$D/prep/run.toml" --manifest "$D/prep/cache/fw.sc.fastdyn.json" --out "$D/bundle.json" > /dev/null
    scopt bundle runtime-identity --run-dir "$D/prep/oracle" --sc "$D/prep/cache/fw.sc" --scm "$D/prep/capture.scm" \
        --out "$D/runtime_identity.json" > /dev/null
    scopt import "$D/prep" --bundle "$D/bundle.json" --runtime-identity "$D/runtime_identity.json" --store "$D/store" --no-progress | tail -1
    SNAP=$(ls -d "$D"/store/snapshots/*)
    scopt admission identity --snapshot "$SNAP" --config "$CFG" --out "$D/runtime_identity.v2.json" > /dev/null
    WS=$("${ARM_PREFIX}nm" "$W/fw/$V/RTOSDemo.out" | awk '$3=="vComputeWindowStart"{print "0x"$1}'); WS=$(printf '0x%x' $(( WS & ~1 )))
    scopt profile run -c "$CFG" --snapshot "$SNAP" --qemu "$Q" --phase "steady@$WS" $(inputs train) --out "$D/prof" > "$D/profile.log"
    echo "$V profile: $(grep -c 'verdict=PASS' "$D/profile.log") runs passed"
    optimize "$V" b3 --b3 --enable specialize-returns --enable chain-returns --enable chain-static
    optimize "$V" exit-pc-fold
    optimize "$V" b2-arena-slide
    scopt b4 package "$SNAP" --enable eliminate-state-sync --store "$D/out/b4" --runtime "$D/runtime_identity.v2.json" > "$D/out/b4.json" 2> "$D/out/b4.err"
    scopt sfb package "$SNAP" --store "$D/out/sfb" > "$D/out/sfb.json" 2> "$D/out/sfb.err"
    compose_one "$V"
    $PIN "$PY" "$T/lib/runs.py" refs "$V" | tail -1
    "$PY" "$T/lib/runs.py" cases "$V"
}

stage_prepare() {       # the two images are independent: prepare them side by side
    local first second failed=0
    prepare_one O0 & first=$!
    prepare_one O0-fb & second=$!
    wait "$first" || failed=1
    wait "$second" || failed=1
    return "$failed"
}

stage_select() {        # rank the candidates on one held-out input, confirm the winner on two others
    local D=$FASTDYN_ROOT/O0-fb C="" n
    for n in b3 exit-pc-fold b2-arena-slide sfb-b3; do C="$C $(ls -d "$D"/out/$n/artifacts/*)"; done
    for n in b4 sfb; do C="$C $("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['path'])" "$D/out/$n.json")"; done
    $PIN scopt tune-warm "$(ls -d "$D"/store/snapshots/*)" $C --training-cases "$D/rank-cases.json" \
        --cases "$D/confirm-cases.json" --training-pairs 12 --pairs 12 --qemu "$Q" --out "$D/tune" --json > "$D/tune.json"
    "$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));print(d['status'], d['reason'], d['selected_artifact'])" "$D/tune.json"
}

stage_bench() {         # bench [REPS]: the timed campaign on the final inputs
    mkdir -p "$W/results"
    flock -n "$W/results/.campaign.lock" $PIN "$PY" "$T/lib/bench.py" "$W/results/campaign.json" "${1:-8}"
}

stage_report() { python3 "$T/lib/report.py" "${1:-$W/results/campaign.json}"; }

stage_all() { stage_firmware; stage_build; stage_firmbolt; stage_bind; stage_prepare; stage_select; stage_bench "$@"; stage_report; }

each() { local w; for w in $DATASET; do TREBUCHET_WORKLOAD=$w TREBUCHET_WORK=$ROOT "$T/trebuchet.sh" fixture "$@"; done; }

stage_dataset() {       # dataset [REPS]: the five numbers for every category
    local w failed=0 pids=()
    exec 8>"$ROOT/.trebuchet-dataset.lock"
    flock -n 8 || { echo "dataset directory is in use" >&2; return 1; }
    TREBUCHET_DATASET_ID=$(python3 "$T/lib/dataset_report.py" "$ROOT" --begin "${1:-8}" $DATASET)
    export TREBUCHET_DATASET_ID
    trap 'if (( $? != 0 )); then python3 "$T/lib/dataset_report.py" "$ROOT" --failed; fi' EXIT
    TREBUCHET_WORK=$ROOT "$T/trebuchet.sh" fixture firmware
    TREBUCHET_WORK=$ROOT "$T/trebuchet.sh" fixture bind
    each build
    each firmbolt
    for w in $DATASET; do       # preparation is not timed: all categories side by side
        TREBUCHET_WORKLOAD=$w TREBUCHET_WORK=$ROOT "$T/trebuchet.sh" fixture prepare > "$ROOT/$w/prepare.log" 2>&1 &
        pids+=($!)
    done
    for w in "${pids[@]}"; do wait "$w" || failed=1; done
    if (( failed )); then echo "a prepare job failed (see the prepare.log files)" >&2; return 1; fi
    for w in $DATASET; do       # timed stages run serially
        TREBUCHET_WORKLOAD=$w TREBUCHET_WORK=$ROOT "$T/trebuchet.sh" fixture select
        TREBUCHET_WORKLOAD=$w TREBUCHET_WORK=$ROOT "$T/trebuchet.sh" fixture bench "$@"
    done
    stage_dataset_report
}

stage_dataset_report() { python3 "$T/lib/dataset_report.py" "$ROOT" $DATASET; }

stage=${1:?usage: trebuchet.sh firmware|build|firmbolt|bind|prepare|compose|select|bench [REPS]|report [FILE]|all [REPS]|dataset [REPS]|dataset_report}
shift
"stage_${stage//-/_}" "$@"
