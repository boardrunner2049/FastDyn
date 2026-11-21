#!/usr/bin/env python3
"""Bind scopt's shipped helper-effect table to this QEMU build.

`scopt helper-effects rebind` refuses when a reviewed source file differs from the tree
the table was written for.  On the QEMU pinned by this branch exactly one does:
include/tcg/tcg.h gained two TCGContext fields (goto_tb_issue_mask outside
CONFIG_DEBUG_TCG, fastdyn_const_pc_promoted).  No TCG_CALL_* flag and no helper
declaration changed, so the rows stay valid.  This script accepts that one difference,
only when the file has the reviewed content, and refuses anything else.

usage: rebind_helper_effects.py QEMU_EXE QEMU_SRC   (needs SCOPT_HELPER_EFFECTS_PATH)
"""
import sys
from scopt.analysis.envlayout import sha256_file
from scopt.analysis.helper_effects import (HelperEffectTable, default_table_path, find_helper_effects,
                                           helper_effects_text, recorded_source_hashes, source_file_hashes)

GATE = "sha256:56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90"
REVIEWED = {"include/tcg/tcg.h": "sha256:eb6ddeff78b98d84c20d9ef66a387feba1d4ce833e266f4d0d185af12b7ef511"}

exe, src = sys.argv[1:3]
old = find_helper_effects(GATE)
want = recorded_source_hashes(old, src); have = source_file_hashes(src, want)
differing = {f for f in want if want[f] != have[f]}
unreviewed = {f for f in differing if REVIEWED.get(f) != have[f]}
if unreviewed:
    sys.exit("helper sources differ and were not reviewed: " + ", ".join(sorted(unreviewed)))
record = dict(old.qemu_source); record["file_sha256"] = dict(sorted(have.items())); record["rebound_from"] = old.exe_sha256
table = HelperEffectTable.build(sha256_file(exe), old.rows, record)
path = default_table_path(table.exe_sha256); path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(helper_effects_text(table), encoding="utf-8")
print(f"{table.table_id} rows {len(table.rows)} -> {path} (accepted reviewed differences: {sorted(differing) or 'none'})")
