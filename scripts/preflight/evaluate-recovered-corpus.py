#!/usr/bin/env python3
"""Reconstruct and evaluate the retained firmware-derived native corpus.

The historical report names three ESCape32 checksum routines and three
FreeRTOS list routines but the raw manifest is unavailable.  This script
extracts the three named ESCape32 routines and all five public functions from
FreeRTOS list.c, retaining that ambiguity in the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Sequence


FUNCTIONS = (
    "crc8",
    "crc8dvbs2",
    "dshotcrc",
    "vListInitialise",
    "vListInitialiseItem",
    "vListInsertEnd",
    "vListInsert",
    "uxListRemove",
)
LEVELS = ("O0", "O1", "O2", "O3", "Os", "Oz")


def run(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=True, text=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_identity(path: Path) -> dict[str, str]:
    root = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"]).stdout.strip()
    head = run(["git", "-C", root, "rev-parse", "HEAD"]).stdout.strip()
    return {"root": root, "head": head}


def extract_function(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"function pattern not found: {pattern}")
    opening = text.find("{", match.start())
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : index + 1]
    raise RuntimeError(f"unterminated function: {pattern}")


def make_source(escape_root: Path, freertos_root: Path) -> tuple[str, dict[str, object]]:
    util = escape_root / "src/util.c"
    io = escape_root / "src/io.c"
    lists = freertos_root / "FreeRTOS/Source/list.c"
    util_text = util.read_text(encoding="utf-8")
    io_text = io.read_text(encoding="utf-8")
    list_text = lists.read_text(encoding="utf-8")

    extracted = [
        extract_function(util_text, r"^char\s+crc8\s*\("),
        extract_function(util_text, r"^char\s+crc8dvbs2\s*\("),
        extract_function(io_text, r"^static\s+int\s+dshotcrc\s*\("),
    ]
    for name, result in zip(FUNCTIONS[:3], extracted):
        if name == "dshotcrc":
            result = result.replace("static int dshotcrc", "NOINLINE int dshotcrc", 1)
        else:
            result = result.replace(f"char {name}", f"NOINLINE char {name}", 1)
        extracted[FUNCTIONS.index(name)] = result

    list_patterns = {
        "vListInitialise": r"^void\s+vListInitialise\s*\(",
        "vListInitialiseItem": r"^void\s+vListInitialiseItem\s*\(",
        "vListInsertEnd": r"^void\s+vListInsertEnd\s*\(",
        "vListInsert": r"^void\s+vListInsert\s*\(",
        "uxListRemove": r"^UBaseType_t\s+uxListRemove\s*\(",
    }
    for name, pattern in list_patterns.items():
        body = extract_function(list_text, pattern)
        body = re.sub(r"^(void|UBaseType_t)(\s+" + name + r"\s*\()", r"NOINLINE \1\2", body, count=1)
        extracted.append(body)

    preamble = r'''#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define NOINLINE __attribute__((noinline,used))
typedef uint32_t TickType_t;
typedef uint32_t UBaseType_t;
#define portMAX_DELAY UINT32_MAX
#define configUSE_MINI_LIST_ITEM 1
#define listSET_FIRST_LIST_ITEM_INTEGRITY_CHECK_VALUE(x) ((void)0)
#define listSET_SECOND_LIST_ITEM_INTEGRITY_CHECK_VALUE(x) ((void)0)
#define listSET_LIST_INTEGRITY_CHECK_1_VALUE(x) ((void)0)
#define listSET_LIST_INTEGRITY_CHECK_2_VALUE(x) ((void)0)
#define listTEST_LIST_ITEM_INTEGRITY(x) ((void)0)
#define listTEST_LIST_INTEGRITY(x) ((void)0)
#define traceENTER_vListInitialise(x) ((void)0)
#define traceRETURN_vListInitialise() ((void)0)
#define traceENTER_vListInitialiseItem(x) ((void)0)
#define traceRETURN_vListInitialiseItem() ((void)0)
#define traceENTER_vListInsertEnd(x,y) ((void)0)
#define traceRETURN_vListInsertEnd() ((void)0)
#define traceENTER_vListInsert(x,y) ((void)0)
#define traceRETURN_vListInsert() ((void)0)
#define traceENTER_uxListRemove(x) ((void)0)
#define traceRETURN_uxListRemove(x) ((void)0)
#define mtCOVERAGE_TEST_DELAY() ((void)0)
#define mtCOVERAGE_TEST_MARKER() ((void)0)
struct xLIST;
typedef struct xLIST_ITEM {
  TickType_t xItemValue;
  struct xLIST_ITEM *pxNext;
  struct xLIST_ITEM *pxPrevious;
  void *pvOwner;
  struct xLIST *pxContainer;
} ListItem_t;
typedef struct xMINI_LIST_ITEM {
  TickType_t xItemValue;
  ListItem_t *pxNext;
  ListItem_t *pxPrevious;
} MiniListItem_t;
typedef struct xLIST {
  volatile UBaseType_t uxNumberOfItems;
  ListItem_t *pxIndex;
  MiniListItem_t xListEnd;
} List_t;
'''

    harness = r'''
static volatile uint64_t sink;
static void reset_list(List_t *list) {
  list->uxNumberOfItems = 0;
  list->pxIndex = (ListItem_t *)&list->xListEnd;
  list->xListEnd.xItemValue = UINT32_MAX;
  list->xListEnd.pxNext = (ListItem_t *)&list->xListEnd;
  list->xListEnd.pxPrevious = (ListItem_t *)&list->xListEnd;
}
static void insert_one(List_t *list, ListItem_t *item) {
  ListItem_t *end = (ListItem_t *)&list->xListEnd;
  item->pxNext = end;
  item->pxPrevious = end;
  item->pxContainer = list;
  end->pxNext = item;
  end->pxPrevious = item;
  list->uxNumberOfItems = 1;
}
static void exercise(int mode, uint64_t iterations, uint32_t seed) {
  unsigned char buffer[96];
  List_t list;
  ListItem_t item;
  for (unsigned i = 0; i < sizeof(buffer); ++i) buffer[i] = (unsigned char)(i * 29u + seed);
  memset(&list, 0, sizeof(list));
  memset(&item, 0, sizeof(item));
  for (uint64_t i = 0; i < iterations; ++i) {
    switch (mode) {
      case 0: sink += (unsigned char)crc8((const char *)buffer, 32 + (i & 31)); break;
      case 1: sink += (unsigned char)crc8dvbs2((const char *)buffer, 32 + (i & 31)); break;
      case 2: sink += (unsigned)dshotcrc((int)(i ^ seed), (int)(i & 1)); break;
      case 3: vListInitialise(&list); sink += list.uxNumberOfItems + list.xListEnd.xItemValue; break;
      case 4: item.pxContainer = &list; vListInitialiseItem(&item); sink += item.pxContainer == NULL; break;
      case 5: reset_list(&list); item.xItemValue = (uint32_t)i; vListInsertEnd(&list, &item); sink += list.uxNumberOfItems; break;
      case 6: reset_list(&list); item.xItemValue = (uint32_t)(i & 0xffff); vListInsert(&list, &item); sink += list.uxNumberOfItems; break;
      case 7: reset_list(&list); insert_one(&list, &item); sink += uxListRemove(&item); break;
      default:
        for (int function = 0; function < 8; ++function) exercise(function, 1, seed + (uint32_t)i);
        break;
    }
  }
}
int main(int argc, char **argv) {
  if (argc != 4) return 64;
  int mode = atoi(argv[1]);
  uint64_t iterations = strtoull(argv[2], NULL, 10);
  uint32_t seed = (uint32_t)strtoul(argv[3], NULL, 10);
  if (mode < 0 || mode > 8 || iterations == 0) return 65;
  exercise(mode, iterations, seed);
  printf("%llu\n", (unsigned long long)sink);
  return 0;
}
'''
    source = preamble + "\n\n".join(extracted) + harness
    provenance = {
        "escape32": git_identity(escape_root),
        "freertos": git_identity(freertos_root),
        "source_files": {
            str(util): sha256(util),
            str(io): sha256(io),
            str(lists): sha256(lists),
        },
        "reconstructed_functions": list(FUNCTIONS),
        "historical_selection_uncertainty": (
            "The historical report identifies three FreeRTOS list routines but its raw manifest "
            "was unavailable; all five public list.c routines are evaluated."
        ),
    }
    return source, provenance


def observe(binary: Path, mode: int, iterations: int, seed: int) -> tuple[int, str]:
    before = time.perf_counter_ns()
    result = run([str(binary), str(mode), str(iterations), str(seed)])
    return time.perf_counter_ns() - before, result.stdout.strip()


def calibrate(binary: Path, mode: int, target_ns: int = 60_000_000) -> int:
    iterations = 256
    while iterations < 1 << 29:
        elapsed, _ = observe(binary, mode, iterations, 101)
        if elapsed >= target_ns:
            return iterations
        scale = max(2, min(16, int(target_ns / max(elapsed, 1))))
        iterations *= scale
    raise RuntimeError(f"could not calibrate mode {mode}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--escape32", type=Path, default=Path("/data/callGraph/ESCape32"))
    parser.add_argument("--freertos", type=Path, default=Path("/data/FreeRTOS"))
    parser.add_argument("--bolt", type=Path, required=True)
    parser.add_argument("--cc", default="clang-18")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=7)
    args = parser.parse_args(argv)
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    source, provenance = make_source(args.escape32.resolve(), args.freertos.resolve())
    fixture = work / "recovered-corpus.c"
    fixture.write_text(source, encoding="utf-8")

    binaries: dict[str, Path] = {}
    compile_commands: dict[str, list[str]] = {}
    for level in LEVELS:
        binary = work / f"corpus-{level}"
        command = [args.cc, f"-{level}", "-g", "-funsigned-char", "-fno-pie", "-no-pie", "-fno-stack-protector", "-Wl,--emit-relocs", str(fixture), "-o", str(binary)]
        run(command)
        binaries[level] = binary
        compile_commands[level] = command

    o0 = binaries["O0"]
    restricted = work / "corpus-O0.bolt-restricted"
    restricted_command = [str(args.bolt.resolve()), str(o0), "--reorder-blocks=none", "--reorder-functions=none", "-o", str(restricted)]
    restricted_log = run(restricted_command)

    profile = work / "training.fdata"
    instrumented = work / "corpus-O0.instrumented"
    instrument_command = [str(args.bolt.resolve()), str(o0), "--instrument", f"--instrumentation-file={profile}", "-o", str(instrumented)]
    instrument_log = run(instrument_command)
    run([str(instrumented), "8", "250000", "7"])
    optimized = work / "corpus-O0.bolt-pgo"
    optimize_command = [str(args.bolt.resolve()), str(o0), f"--data={profile}", "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort", "--split-functions", "--split-all-cold", "-o", str(optimized)]
    optimize_log = run(optimize_command)

    stoke_csv = work / "stoke.csv"
    stoke_command = [str(args.bolt.resolve()), str(o0), "--stoke", f"--stoke-out={stoke_csv}", "--print-stoke", "-o", str(work / "corpus-O0.stoke-info")]
    stoke_log = run(stoke_command)

    variants = {**binaries, "O0_bolt_restricted": restricted, "O0_bolt_pgo": optimized}
    iterations = {FUNCTIONS[mode]: calibrate(o0, mode) for mode in range(len(FUNCTIONS))}
    timings: dict[str, object] = {}
    for mode, name in enumerate(FUNCTIONS):
        count = iterations[name]
        reference_outputs = {str(seed): observe(o0, mode, min(count, 4096), seed)[1] for seed in (11, 13, 17)}
        function_result: dict[str, object] = {"iterations": count, "variants": {}}
        for variant, binary in variants.items():
            for seed, reference in reference_outputs.items():
                candidate = observe(binary, mode, min(count, 4096), int(seed))[1]
                if candidate != reference:
                    raise RuntimeError(f"output mismatch: {name} {variant} seed {seed}")
            function_result["variants"][variant] = {"samples_ns": []}
        variant_items = list(variants.items())
        for sample in range(args.samples):
            seed = (11, 13, 17)[sample % 3]
            offset = sample % len(variant_items)
            order = variant_items[offset:] + variant_items[:offset]
            if sample % 2:
                order.reverse()
            for variant, binary in order:
                elapsed, _ = observe(binary, mode, count, seed)
                function_result["variants"][variant]["samples_ns"].append(elapsed)
        for item in function_result["variants"].values():
            item["median_ns"] = statistics.median(item["samples_ns"])
        baseline = function_result["variants"]["O0"]["median_ns"]
        for item in function_result["variants"].values():
            item["o0_over_variant"] = baseline / item["median_ns"]
        timings[name] = function_result

    combined_count = calibrate(o0, 8)
    combined_outputs = {
        str(seed): observe(o0, 8, min(combined_count, 4096), seed)[1]
        for seed in (11, 13, 17)
    }
    combined_variants: dict[str, object] = {
        variant: {"samples_ns": []} for variant in variants
    }
    variant_items = list(variants.items())
    for sample in range(args.samples):
        seed = (11, 13, 17)[sample % 3]
        offset = sample % len(variant_items)
        order = variant_items[offset:] + variant_items[:offset]
        if sample % 2:
            order.reverse()
        for variant, binary in order:
            candidate = observe(binary, 8, min(combined_count, 4096), seed)[1]
            if candidate != combined_outputs[str(seed)]:
                raise RuntimeError(f"combined output mismatch: {variant} seed {seed}")
            elapsed, _ = observe(binary, 8, combined_count, seed)
            combined_variants[variant]["samples_ns"].append(elapsed)
    for item in combined_variants.values():
        item["median_ns"] = statistics.median(item["samples_ns"])
    combined_baseline = combined_variants["O0"]["median_ns"]
    for item in combined_variants.values():
        item["o0_over_variant"] = combined_baseline / item["median_ns"]
    timings["combined_all"] = {
        "iterations": combined_count,
        "variants": combined_variants,
    }

    report = {
        "schema_version": 1,
        "classification": "recovered_firmware_derived_native_corpus",
        "provenance": provenance,
        "host": {"uname": list(os.uname()), "cc": run([args.cc, "--version"]).stdout.splitlines()[0]},
        "commands": {"compile": compile_commands, "bolt_restricted": restricted_command, "bolt_instrument": instrument_command, "bolt_optimize": optimize_command, "bolt_stoke_info": stoke_command},
        "artifacts": {name: {"sha256": sha256(path), "bytes": path.stat().st_size} for name, path in variants.items()},
        "stoke_info": {"csv": stoke_csv.read_text(encoding="utf-8").splitlines(), "log": stoke_log.stdout.splitlines()},
        "bolt_logs": {"restricted": restricted_log.stdout.splitlines(), "instrument": instrument_log.stdout.splitlines(), "optimize": optimize_log.stdout.splitlines()},
        "timings": timings,
        "claims": {"native_x86_only": True, "cortex_m_rewrite": False, "fastdyn_speedup": False, "hardware_cycles": False},
    }
    report_path = work / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "functions": list(FUNCTIONS), "iterations": iterations}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
