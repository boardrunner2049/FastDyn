#!/usr/bin/env bash
# Export available immutable offline inputs, including incomplete preparation.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../../../.." && pwd)
work=$(realpath "${1:?campaign workspace}")
results=$(realpath "${2:?results}")
qemu=$(realpath -m "${3:?QEMU binary}")
mkdir "$results/training"
dst="$results/training"
shopt -s nullglob
pack() {
    local name=$1 dir=$2
    shift 2
    tar --sparse -czf - -C "$dir" "$@" | split -b 128M -d -a 4 - "$dst/$name.tar.gz.part-"
}
if [[ -d "$work/campaign" ]]; then
    cd "$work"
    for variant in original firmbolt; do
        source=campaign/$variant
        [[ -d $source ]] || continue
        files=()
        snapshots=("$source"/store/snapshots/*/snapshot.json)
        if (( ! ${#snapshots[@]} )); then
            # The importer may have failed on a completed capture. Retain the
            # original sparse cache and SCM inputs to reproduce that failure.
            for raw in "$source"/prep/cache/fw.sc "$source"/prep/capture*.scm; do
                [[ ! -f "$raw" ]] || files+=("$raw")
            done
        fi
        for path in "$source"/store/snapshots/* "$source"/profile/merged.profile.json \
            "$source"/profile/profile_run.json "$source"/profile/*/profile.json \
            "$source"/bundle.json "$source"/runtime.json "$source"/admission-runtime.json \
            "$source"/pye.toml "$source"/prep/cache/fw.sc.fastdyn.json "$source"/prep/prep.json \
            campaign/{envlayout,helper-effects} campaign/{contract,campaign,policy}.json; do
            [[ ! -e "$path" ]] || files+=("$path")
        done
        if [[ $variant = original ]]; then
            elf=product/micropython/ports/qemu/build-MPS2_AN385/firmware.elf
        else
            elf=campaign/firmbolt-guest/firmware.elf
        fi
        [[ ! -f $elf ]] || files+=("$elf")
        if (( ${#files[@]} )); then pack "$variant" "$work" "${files[@]}"; fi
        if [[ -d "$source/store/snapshots" && -f "$source/profile/merged.profile.json" ]]; then
            echo "$variant snapshot and profile present"
            profile="$source/profile/merged.profile.json"
            census="$dst/$variant-site-census.json"
            # A same-host offline read preserves the executable path and inode
            # checks in the native image resolver. All trial processes have exited.
            python="$repo/scopt-env/bin/python"
            script="$repo/tools/trebuchet/lib/site_census.py"
            census_log="$dst/$variant-site-census.log"
            get_id='from scopt.formats.profile_v1 import read_profile; import sys; print(read_profile(sys.argv[1]).snapshot_id.split(":")[1])'
            if snapshot=$("$python" -c "$get_id" "$profile" 2>"$census_log") &&
                SCOPT_ENVLAYOUT_PATH="$work/campaign/envlayout" \
                SCOPT_HELPER_EFFECTS_PATH="$work/campaign/helper-effects" \
                "$python" "$script" --snapshot "$source/store/snapshots/$snapshot" \
                    --elf "$elf" --profile "$profile" --application pye \
                    --scenario update-package-version --phase steady --out "$census" 2>>"$census_log"; then
                gzip "$census"
                echo "$variant same-host site census complete"
            else
                echo "$variant same-host site census unavailable; see $variant-site-census.log"
            fi
        else
            echo "$variant incomplete preparation; see campaign receipt"
        fi
    done > "$dst/status.txt"
    if [[ -f $qemu ]]; then
        pack runtime "$(dirname "$qemu")" "$(basename "$qemu")"
        sha256sum "$qemu" > "$dst/qemu.sha256"
    fi
fi
cd "$dst"
parts=(*.part-*)
if (( ${#parts[@]} )); then
    sha256sum "${parts[@]}" > SHA256SUMS
    stat --printf='%s %n\n' "${parts[@]}" > sizes.txt
fi
