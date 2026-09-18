#!/usr/bin/env bash
# Source this file from a reproduction launcher; it deliberately does not
# depend on a target being registered in boardrunner_sdk/CMakeLists.txt.

build_repro_model() {
    local source=$1
    local output=$2
    local fastdyn_root=$3
    local workspace_root=$4
    local variant=$5
    local sdk_build fastdyn_build

    case $variant in
        normal)
            sdk_build="$fastdyn_root/boardrunner/boardrunner_sdk/build"
            fastdyn_build="$fastdyn_root/build"
            ;;
        aflnet)
            sdk_build="$fastdyn_root/boardrunner/boardrunner_sdk/build-aflnet"
            fastdyn_build="$fastdyn_root/build-aflnet"
            ;;
        *)
            printf 'Unknown reproducibility model variant: %s\n' "$variant" >&2
            return 2
            ;;
    esac

    [[ -f $source ]] || { printf 'Missing bundled model source: %s\n' "$source" >&2; return 2; }
    [[ -f $sdk_build/libboardrunner_vio.so ]] || {
        printf 'Missing BoardRunner VIO runtime: %s\n' "$sdk_build/libboardrunner_vio.so" >&2
        return 2
    }
    [[ -f $fastdyn_build/libfastdyn.so ]] || {
        printf 'Missing FastDyn runtime: %s\n' "$fastdyn_build/libfastdyn.so" >&2
        return 2
    }
    command -v cc >/dev/null 2>&1 || { printf 'Missing C compiler (cc).\n' >&2; return 2; }
    command -v pkg-config >/dev/null 2>&1 || { printf 'Missing pkg-config.\n' >&2; return 2; }
    pkg-config --exists glib-2.0 || { printf 'glib-2.0 development files are required.\n' >&2; return 2; }

    mkdir -p "$(dirname -- "$output")"
    cc -shared -fPIC -g -o "$output" "$source" \
        -I"$fastdyn_root/boardrunner/boardrunner_sdk/include" \
        -I"$fastdyn_root/include" -I"$workspace_root/qemu/include" \
        $(pkg-config --cflags glib-2.0) \
        -L"$sdk_build" -lboardrunner_vio \
        -L"$fastdyn_build" -lfastdyn $(pkg-config --libs glib-2.0) \
        -Wl,-rpath,"$sdk_build:$fastdyn_build"
}
