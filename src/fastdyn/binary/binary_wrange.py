#!/usr/bin/env python3
import logging
import os
import sys
from dataclasses import dataclass

from elftools.elf.constants import P_FLAGS
from elftools.elf.elffile import ELFFile

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------------
# This script takes a binary and outputs both all writable ranges and the
# user-selected subset used by the fuzzing snapshot.
# -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionMapping:
    name: str
    address: int
    size: int
    section_type: str


@dataclass(frozen=True)
class WritableSegment:
    index: int
    segment_type: str
    address: int
    file_size: int
    memory_size: int
    flags: int
    sections: list[SectionMapping]


def _format_flags(flags):
    flag_order = (
        (P_FLAGS.PF_R, "R"),
        (P_FLAGS.PF_W, "W"),
        (P_FLAGS.PF_X, "X"),
    )
    value = "".join(name for mask, name in flag_order if flags & mask)
    return value or "-"


def _format_range(start, size):
    return f"0x{start:08X}-0x{start + size:08X}"


def _sections_for_segment(elf, segment):
    sections = []

    for section in elf.iter_sections():
        if not segment.section_in_segment(section):
            continue

        name = section.name or "<unnamed>"
        sections.append(
            SectionMapping(
                name=name,
                address=section["sh_addr"],
                size=section["sh_size"],
                section_type=section["sh_type"],
            )
        )

    return sections


def collect_writable_segments(elf_path):
    writable = []

    with open(elf_path, "rb") as f:
        elf = ELFFile(f)

        for index, segment in enumerate(elf.iter_segments()):
            if not segment["p_flags"] & P_FLAGS.PF_W:
                continue
            if segment["p_memsz"] == 0:
                continue

            writable.append(
                WritableSegment(
                    index=index,
                    segment_type=segment["p_type"],
                    address=segment["p_vaddr"],
                    file_size=segment["p_filesz"],
                    memory_size=segment["p_memsz"],
                    flags=segment["p_flags"],
                    sections=_sections_for_segment(elf, segment),
                )
            )

    return writable


def print_writable_segments(segments):
    if not segments:
        log.info("[binary_wrange.py] No writable segments found.")
        return

    log.info("[binary_wrange.py] Writable segments:")
    for selection_id, segment in enumerate(segments):
        log.info(
            f"  [{selection_id}] segment {segment.index}: "
            f"{segment.segment_type} {_format_flags(segment.flags)} "
            f"{_format_range(segment.address, segment.memory_size)} "
            f"memsz=0x{segment.memory_size:x} filesz=0x{segment.file_size:x}"
        )

        if segment.sections:
            log.info("      sections:")
            for section in segment.sections:
                log.info(
                    f"        - {section.name} "
                    f"{_format_range(section.address, section.size)} "
                    f"size=0x{section.size:x} type={section.section_type}"
                )
        else:
            log.info("      sections: <none>")


def _parse_selection(selection, count):
    selection = selection.strip().lower()
    if selection in ("", "a", "all", "*"):
        return list(range(count))
    if selection in ("n", "none"):
        return []

    selected = set()
    tokens = selection.replace(",", " ").split()
    for token in tokens:
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text, 10)
            end = int(end_text, 10)
            if start > end:
                start, end = end, start
            indexes = range(start, end + 1)
        else:
            indexes = (int(token, 10),)

        for index in indexes:
            if index < 0 or index >= count:
                raise ValueError(
                    f"selection {index} is outside the valid range 0-{count - 1}"
                )
            selected.add(index)

    return sorted(selected)


def prompt_for_segments(segments):
    print_writable_segments(segments)

    if not segments:
        return []

    configured_selection = os.environ.get("FASTDYN_SNAPSHOT_SEGMENTS")
    if configured_selection is not None:
        try:
            selected = _parse_selection(configured_selection, len(segments))
        except ValueError as exc:
            raise ValueError(
                "FASTDYN_SNAPSHOT_SEGMENTS must be 'all', 'none', or a "
                f"valid segment expression: {exc}"
            ) from exc

        log.info(
            "[binary_wrange.py] Selecting writable segments from "
            "FASTDYN_SNAPSHOT_SEGMENTS=%r.",
            configured_selection,
        )
        return selected

    # Automated invocations have no one available to answer the prompt.  The
    # historical EOF fallback already chose all ranges; detect this condition
    # before printing a prompt so wrapper scripts cannot spend their entire
    # timeout in input().
    if not sys.stdin.isatty():
        log.info(
            "[binary_wrange.py] Non-interactive stdin; including all "
            "writable segments. Set FASTDYN_SNAPSHOT_SEGMENTS to override."
        )
        return list(range(len(segments)))

    prompt = (
        "Select writable segments to include in the snapshot "
        "(e.g. 0,2-3; blank/all for all; none for none): "
    )

    while True:
        try:
            selection = input(prompt)
        except EOFError:
            log.info(
                "[binary_wrange.py] No input provided; including all segments."
            )
            return list(range(len(segments)))

        try:
            return _parse_selection(selection, len(segments))
        except ValueError as e:
            log.info(f"[binary_wrange.py] Invalid selection: {e}")


def write_segments(out_file, segments, indexes):
    with open(out_file, "w") as f:
        for index in indexes:
            segment = segments[index]
            f.write(f"0x{segment.address:08X}\t0x{segment.memory_size:x}\n")


# Write tab-delimited address + size files for all writable ranges and the
# selected snapshot subset. The selected file retains its historical name.
def run(selected_out_file, bin_path, all_out_file=None):
    if all_out_file is None:
        all_out_file = os.path.join(
            os.path.dirname(selected_out_file), "bin-all-writable-ranges"
        )

    selected_exists = os.path.exists(selected_out_file)
    all_exists = os.path.exists(all_out_file)
    if selected_exists and all_exists:
        log.info(
            "[binary_wrange.py] Skipping existing writable-range files: "
            f"{selected_out_file}, {all_out_file}"
        )
        return

    try:
        segments = collect_writable_segments(bin_path)

        if not all_exists:
            write_segments(all_out_file, segments, range(len(segments)))
        else:
            log.info(
                f"[binary_wrange.py] Skipping existing output file: {all_out_file}"
            )

        if not selected_exists:
            selected_indexes = prompt_for_segments(segments)
            write_segments(selected_out_file, segments, selected_indexes)
        else:
            log.info(
                f"[binary_wrange.py] Skipping existing output file: {selected_out_file}"
            )
    except Exception as e:
        log.info(
            "[binary_wrange.py] Couldn't write writable-range files "
            f"({selected_out_file}, {all_out_file}): {e}"
        )


if __name__ == "__main__":
    from fastdyn import logs
    logs.configure()

    if len(sys.argv) != 2:
        sys.exit("Usage: python binary_wrange.py <firmware.elf>")

    run("fastdyn_work/bin-writable-ranges", sys.argv[1])
