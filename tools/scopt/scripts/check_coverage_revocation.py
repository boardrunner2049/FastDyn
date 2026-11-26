#!/usr/bin/env python3
"""Exercise QEMU's permanent coverage disarm on a real armed semantic SuperCache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from scopt.canon import canonical_json
from scopt.formats.sc_semantic import read_semantic_cache
from scopt.importer.import_cache import open_snapshot
from scopt.run.deploy import deploy
from scopt.run.launcher import create_run_dir, launch


def run(qualification: Path, out: Path, trigger: str) -> None:
    data = json.loads(qualification.read_text())
    artifact = Path(data["arms"]["complete"]["artifact"])
    qemu, config, in_file = (Path(data[name]) for name in ("qemu", "config", "heldout"))
    os.environ["SCOPT_ENVLAYOUT_PATH"] = str(qualification.parent / "envlayouts")
    os.environ["SCOPT_RUNTIME_QEMU"] = str(qemu)
    with qemu.open("rb") as stream:
        os.environ["SCOPT_RUNTIME_QEMU_SHA256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    source = read_semantic_cache(artifact / "cache.sc")
    snap = open_snapshot(data["snapshot"])
    pc = data[f"{trigger}_pc"]
    keys = {u.key for u in snap.units() if pc in u.vaddrs}
    if (not source.loadable or source.ext is None or not source.ext.links
            or source.ext.checksum is None or not keys):
        raise ValueError("need a linked semantic table and exact observed translation keys")

    out.mkdir(parents=True)
    guard = out / "guard.txt"
    lines = [f"scopt-runtime-coverage-v1 {pc:x} {pc + 2:x} "
             f"{source.sc.header.runtime_signature:x} {source.ext.checksum:x}"]
    for key in sorted(keys, key=lambda k: (k.page_addr0, k.page_addr1_raw, k.flags, k.cs_base, k.cflags)):
        lines.append(f"{pc:x} {key.page_addr0:x} {key.page_addr1_raw:x} "
                     f"{key.flags:x} {key.cs_base:x} {key.cflags:x}")
    guard.write_text("\n".join(lines) + "\n")
    original_count = len(source.sc.valid_slots)
    expected = next(r["baseline"]["guest_norm"] for r in
                    json.loads(Path(data["arms"]["complete"]["dynamic"]).read_text())["runs"]
                    if Path(r["input"]) == in_file)

    arms = {}
    for arm in ("control", "unexpected-key"):
        root = out / arm
        rd = create_run_dir(config, root, qemu=qemu, label=arm)
        deploy(artifact, rd)
        stats_path = root / "v6-stats.json"
        variables = {"QEMU_SCOPT_COVERAGE_GUARD": str(guard), "QEMU_SC_V6_STATS": str(stats_path)}
        if arm == "unexpected-key":
            variables["QEMU_SCOPT_COVERAGE_TEST_UNEXPECTED"] = f"{pc:x}"
        doc = launch(rd, inputs={"input": in_file}, timeout_s=90,
                     extra_env=variables, prove=False)
        if doc["exit_code"] != 0 or doc["timed_out"]:
            raise ValueError(f"{arm} run failed")
        actual = doc["guest_norm"]
        if actual != expected:
            raise ValueError(f"{arm} guest output differs from checked baseline")
        stats = json.loads(stats_path.read_text())
        sc = read_semantic_cache(rd.cache)
        appended = len(sc.sc.valid_slots) - original_count
        if (not sc.loadable or not stats["loaded"] or stats["invariant_violations"]
                or stats["links_armed"] < 1):
            raise ValueError(f"{arm} did not execute with a clean armed semantic chain")
        if sc.sc.record_bytes[:original_count] != source.sc.record_bytes:
            raise ValueError(f"{arm} changed the original cache records")
        new_keys = [r.key for r in sc.sc.records[original_count:] if r is not None]
        has_unexpected_key = any(
            key.cflags & 0x1FF == 2
            and any((key.page_addr0, key.page_addr1, key.cs_base, key.flags) ==
                    (old.page_addr0, old.page_addr1, old.cs_base, old.flags) for old in keys)
            for key in new_keys
        )
        arms[arm] = {"stats": stats, "run": str(root / "run.json"), "appended": appended,
                     "appended_unexpected_key": has_unexpected_key}

    control, failed = arms["control"], arms["unexpected-key"]
    before, after = control["stats"], failed["stats"]
    if control["appended"] or before["coverage_violations"] or not before["variant_exits"]:
        raise ValueError("coverage-free control did not execute its unchanged specialized chain")
    if (after["coverage_test_injections"] != 1 or after["coverage_violations"] != 1
            or after["forbid_reason"] != "coverage_violation" or after["disarm_forbid_links"] < 1
            or any(link["armed"] for link in after["links"])
            or after["variant_exits"] != after["coverage_variant_exits_at_revocation"]
            or not failed["appended_unexpected_key"]):
        raise ValueError("injected new key did not permanently disarm the optimized chain")

    bad_guard = out / "stale-guard.txt"
    lines[0] = lines[0].rsplit(" ", 1)[0] + f" {source.ext.checksum ^ 1:x}"
    bad_guard.write_text("\n".join(lines) + "\n")
    rd = create_run_dir(config, out / "stale-guard", qemu=qemu, label="stale-guard")
    deploy(artifact, rd)
    doc = launch(rd, inputs={"input": in_file}, timeout_s=30,
                 extra_env={"QEMU_SCOPT_COVERAGE_GUARD": str(bad_guard)}, prove=False)
    log = Path(doc["stdout_log"]).read_text(errors="replace")
    if doc["exit_code"] == 0 or "coverage guard identity or range differs" not in log:
        raise ValueError("stale coverage guard was not refused on startup")
    result = {"schema": "scopt.coverage_revocation_test", "version": 1,
              "artifact": str(artifact), "pc": pc, "arms": arms, "stale_guard_refused": True}
    (out / "revocation.json").write_bytes(canonical_json(result) + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--trigger", choices=("source", "consumer"), default="consumer")
    args = parser.parse_args()
    run(args.qualification.resolve(strict=True), args.out.resolve(), args.trigger)


if __name__ == "__main__":
    main()
