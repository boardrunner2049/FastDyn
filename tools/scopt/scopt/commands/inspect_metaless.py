"""``scopt inspect metaless``: meta-less reference classification of a ``.sc`` (WP2).

The ``inspect`` group is shared with other work packages (design §5.11:
``inspect unit|ref|edges|diff``). :func:`inspect_group` gets the group if
another module already registered it, or creates it, so modules can register
in any order.
"""

from __future__ import annotations

import json
import sys

import click


def inspect_group(group: click.Group) -> click.Group:
    """The shared ``inspect`` subgroup of ``group`` (created on first use)."""
    g = group.commands.get("inspect")
    if isinstance(g, click.Group):
        return g

    @click.group("inspect")
    def inspect() -> None:
        """Inspect snapshots, units and references."""

    group.add_command(inspect)
    return inspect


def register(group: click.Group) -> None:
    inspect_group(group).add_command(metaless)


def _int(s: str | None) -> int | None:
    return None if s is None else int(s, 0)


@click.command("metaless")
@click.argument("sc_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--qemu", default=None, type=click.Path(exists=True, dir_okay=False),
              help="QEMU binary for helper symbols (default: the maintainer's B0 build, "
                   "else the checkout's third_party/common/qemu/build/qemu-system-arm).")
@click.option("--qemu-base", default="0x555555554000", show_default=True,
              help="QEMU PIE load bias (setarch -R).")
@click.option("--plugin", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Plugin .so (default: the maintainer's B0 build, else the checkout's "
                   "build/libfastdyn.so).")
@click.option("--plugin-base", default=None,
              help="Plugin load base (default: inferred from external call targets).")
@click.option("--maps", "maps_path", default=None, type=click.Path(exists=True, dir_okay=False),
              help="/proc/<pid>/maps of the run that wrote the .sc; replaces both base guesses.")
@click.option("--tb-ret-off", default=None, help="Arena offset of tb_ret_addr (default: inferred).")
@click.option("--epilogue-off", default=None,
              help="Arena offset of tcg_code_gen_epilogue (default: inferred).")
@click.option("--firmware", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Firmware ELF: bind guest bytes by guest_hash and check Thumb lengths.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--refs", "with_refs", is_flag=True, help="With --json: every reference per TB.")
@click.option("--expect-g17", is_flag=True,
              help="Exit 1 unless the tallies equal design G17 (minimal.sc).")
def metaless(sc_path: str, qemu: str | None, qemu_base: str, plugin: str | None,
             plugin_base: str | None, maps_path: str | None, tb_ret_off: str | None,
             epilogue_off: str | None, firmware: str | None, as_json: bool, with_refs: bool,
             expect_g17: bool) -> None:
    """Classify every native reference in SC_PATH without a capture (bring-up only).

    Prints class tallies, the design G17 line
    (helper pool calls / plugin rel32 calls / external RIP LEAs / tb_ret jmps /
    epilogue jmps / own-struct LEAs (+idx) / raddr LEAs / goto_ptr / movabs)
    and every reference it could not explain. Exit status: 0, or 1 if a
    reference is unknown, a TB does not decode, or --expect-g17 fails.
    """
    from ..decode.symbols import Maps
    from ..importer.metaless import DEFAULT_PLUGIN, DEFAULT_QEMU, G17, classify_sc, format_g17

    res = classify_sc(
        sc_path, qemu=qemu or DEFAULT_QEMU, qemu_base=int(qemu_base, 0),
        plugin=plugin or DEFAULT_PLUGIN, plugin_base=_int(plugin_base),
        maps=Maps.load(maps_path) if maps_path else None, tb_ret_off=_int(tb_ret_off),
        epilogue_off=_int(epilogue_off), firmware=firmware)
    doc = res.to_json(refs=with_refs)
    failed = bool(doc["unknown"]) or doc["g17"]["bad"] > 0 or any(
        not t.tiled for t in res.tbs) or (expect_g17 and not doc["g17_match"])
    if as_json:
        json.dump(doc, sys.stdout, indent=1)
        sys.stdout.write("\n")
        sys.exit(1 if failed else 0)

    w = click.echo
    w(f"sc: {res.sc_path}")
    w(f"code_address 0x{res.code_address:x}  prologue_end 0x{res.prologue_end:x}  "
      f"TBs {len(res.tbs)}  skipped {len(res.skipped_slots)}")
    for m in doc["modules"]:
        w(f"module {m['name']}: base {m['base']} ({m['source']})")
    for n in res.prologue_notes:
        w(f"prologue: {n}")
    w("")
    w("class tallies (operand kind:class->binding):")
    for k, v in doc["kind_classes"].items():
        w(f"  {v:6d}  {k}")
    w("")
    w("helpers called through the pool:")
    for k, v in doc["helpers"].items():
        w(f"  {v:6d}  {k}")
    g = doc["g17"]
    w("")
    w(f"(bad) instructions: {g['bad']}   TBs with the irq check: {doc['irq_check']}/"
      f"{len(res.tbs)}   host-pointer imm32/disp32: {g['host_ptr_imm32']}   "
      f"distinct helpers: {g['helper_names']}")
    w(f"G17: {doc['g17_line']}")
    w(f"     expected {format_g17(G17)}: {'MATCH' if doc['g17_match'] else 'DIFFERENT'}")
    if doc["guest"]:
        gs = doc["guest"]
        w(f"guest: firmware delta {gs['delta']}  elf_verified {gs['elf_verified']}  "
          f"thumb lengths ok {gs.get('thumb_lengths_ok', 0)}")
    if doc["unknown"]:
        w(f"\nunknown references ({len(doc['unknown'])}):")
        for u in doc["unknown"][:50]:
            w(f"  slot {u['slot']} field +0x{u['field_off']:x} {u['kind']} target {u['target']} "
              f"{u.get('detail', '')} {u.get('symbol', '')}")
    for t in doc["tbs_with_problems"][:50]:
        w(f"slot {t['slot']}: {', '.join(t['problems'])}")
    sys.exit(1 if failed else 0)
