"""Exercise the actual direct-slot overlay functions without a fixed-address VM.

The C fixture links the system GLib implementation. Only the unchanged QEMU arm
operation is replaced by a recorder, so these tests cover index routing and
live-identity/epoch gates rather than claiming to validate QEMU invalidation.
"""
from __future__ import annotations

import ctypes.util
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


OVERLAY = Path(__file__).resolve().parents[2] / "patches/qemu-continuity-direct.patch"


def added_function(name: str) -> str:
    added = "\n".join(line[1:] for line in OVERLAY.read_text().splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    match = re.search(r"^(?:static )?(?:void|bool) " + name + r"\([^;]*?\)\n\{",
                      added, re.MULTILINE)
    assert match, f"the overlay must include the complete {name} implementation"
    depth = 1
    end = match.end()
    while depth:
        if added[end] == "{":
            depth += 1
        elif added[end] == "}":
            depth -= 1
        end += 1
    return added[match.start():end]


HARNESS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* Stable public GLib ABI, allowing this small test without its dev headers. */
typedef struct _GHashTable GHashTable;
extern unsigned g_direct_hash(const void *key);
extern int g_direct_equal(const void *a, const void *b);
extern GHashTable *g_hash_table_new(unsigned (*hash)(const void *),
                                  int (*equal)(const void *, const void *));
extern int g_hash_table_insert(GHashTable *, void *key, void *value);
extern void *g_hash_table_lookup(GHashTable *, const void *key);
extern void g_hash_table_unref(GHashTable *);

#define SC6_EXIT_DIRECT 3
typedef uint64_t vaddr;
typedef struct TranslationBlock { bool invalid; } TranslationBlock;
typedef struct CPUState { bool forbidden; } CPUState;
typedef struct Sc6Slot {
    struct {
        uint64_t src_tb_offset;
        uint32_t target_vaddr;
        uint8_t exit_class, slot_n;
    } e;
    uint32_t src;
} Sc6Slot;
typedef struct Sc6Variant { uint32_t general; } Sc6Variant;
typedef struct Sc6Link {
    uint32_t slot, variant;
    bool armed, disarmed_epoch;
} Sc6Link;
static struct {
    uint32_t n_links;
    Sc6Link *links;
    Sc6Slot *slots;
    Sc6Variant *variants;
    TranslationBlock **live;
    Sc6Link *direct_single[2];
    GHashTable *direct_links[2];
} sc6;
static struct { void *buffer; } supercache;
static unsigned calls, last_link;
static bool tcg_sc6_has_direct;

/* Arm legality remains the responsibility of QEMU's existing sc6_try_arm. */
static void sc6_try_arm(CPUState *cpu, uint32_t index)
{
    Sc6Link *link = &sc6.links[index];
    const Sc6Slot *slot = &sc6.slots[link->slot];
    const Sc6Variant *v = &sc6.variants[link->variant];
    calls++;
    last_link = index;
    if (!link->disarmed_epoch && !cpu->forbidden &&
        !sc6.live[slot->src]->invalid && !sc6.live[v->general]->invalid) {
        link->armed = true;
    }
}

@FUNCTIONS@

enum { LINKS = 4096, TB_COUNT = 8, STRIDE = 64 };

static TranslationBlock *tb(unsigned n)
{
    return (TranslationBlock *)((uint8_t *)supercache.buffer + n * STRIDE);
}

static void setup(void)
{
    supercache.buffer = calloc(TB_COUNT, STRIDE);
    sc6.links = calloc(LINKS, sizeof(*sc6.links));
    sc6.slots = calloc(LINKS, sizeof(*sc6.slots));
    sc6.variants = calloc(LINKS, sizeof(*sc6.variants));
    sc6.live = calloc(TB_COUNT, sizeof(*sc6.live));
    assert(supercache.buffer && sc6.links && sc6.slots && sc6.variants && sc6.live);
    sc6.n_links = LINKS;
    for (unsigned i = 0; i < TB_COUNT; i++) {
        sc6.live[i] = tb(i);
    }
    for (unsigned i = 0; i < LINKS; i++) {
        sc6.links[i].slot = sc6.links[i].variant = i;
        sc6.slots[i].e.exit_class = 2;
    }
}

static void direct(unsigned i, unsigned source, unsigned slot,
                   unsigned target, uint32_t pc)
{
    tcg_sc6_has_direct = true;
    sc6.slots[i].e.exit_class = SC6_EXIT_DIRECT;
    sc6.slots[i].e.src_tb_offset = source * STRIDE;
    sc6.slots[i].e.slot_n = slot;
    sc6.slots[i].e.target_vaddr = pc;
    sc6.slots[i].src = source;
    sc6.variants[i].general = target;
}

static void cleanup(void)
{
    for (unsigned i = 0; i < 2; i++) {
        if (sc6.direct_links[i]) {
            g_hash_table_unref(sc6.direct_links[i]);
        }
    }
    free(sc6.links);
    free(sc6.slots);
    free(sc6.variants);
    free(sc6.live);
    free(supercache.buffer);
    memset(&sc6, 0, sizeof(sc6));
    calls = 0;
    tcg_sc6_has_direct = false;
}

int main(void)
{
    CPUState cpu = {0};
    setup();
    sc6_index_direct_links();
    assert(!sc6.direct_links[0] && !sc6.direct_links[1] &&
           !sc6.direct_single[0] && !sc6.direct_single[1]);
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    assert(!calls);
    cleanup();

    setup();
    direct(11, 0, 0, 1, 0x100);
    direct(2121, 0, 1, 2, 0x200);
    direct(LINKS - 1, 3, 0, 4, 0x300);
    direct(1000, 5, 0, 6, 0x400);
    sc6_index_direct_links();
    assert(sc6.direct_links[0] && !sc6.direct_single[0]);
    assert(!sc6.direct_links[1] && sc6.direct_single[1] == &sc6.links[2121]);
    assert(tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    assert(last_link == 11 && calls == 1);
    assert(tcg_supercache_link_direct(&cpu, tb(0), 1, tb(2), 0x200));
    assert(last_link == 2121 && calls == 2);
    assert(tcg_supercache_link_direct(&cpu, tb(3), 0, tb(4), 0x300));
    assert(last_link == LINKS - 1 && calls == 3);

    /* Misses and wrong identities must not reach the arming operation. */
    assert(!tcg_supercache_link_direct(&cpu, tb(0), -1, tb(1), 0x100));
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 2, tb(1), 0x100));
    assert(!tcg_supercache_link_direct(&cpu, tb(5), 0, tb(1), 0x100));
    assert(!tcg_supercache_link_direct(&cpu, tb(5), 1, tb(2), 0x200));
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x104));
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 0, tb(2), 0x100));
    assert(calls == 3);

    /* A cache flush clears live identities, but the immutable index survives. */
    sc6.links[11].armed = false;
    sc6.live[0] = NULL;
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    sc6.live[0] = tb(0);
    sc6.live[1] = NULL;
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    assert(calls == 3);
    sc6.live[1] = tb(1);
    assert(tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    assert(last_link == 11 && calls == 4);

    /* Cached addresses alone cannot override disarm, invalidation or debug. */
    sc6.links[11].armed = false;
    sc6.links[11].disarmed_epoch = true;
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    sc6.links[11].disarmed_epoch = false;
    tb(1)->invalid = true;
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    tb(1)->invalid = false;
    cpu.forbidden = true;
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    cpu.forbidden = false;

    /* Lookup must not walk an unrelated link after the index is built. */
    sc6.links[0].slot = UINT32_MAX;
    assert(tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    assert(last_link == 11);
    assert(!tcg_supercache_link_direct(&cpu, tb(6), 0, tb(1), 0x100));
    assert(tcg_supercache_link_direct(&cpu, tb(5), 0, tb(6), 0x400));
    assert(last_link == 1000);
    cleanup();

    setup();
    direct(11, 0, 1, 1, 0x100);
    sc6_index_direct_links();
    assert(!sc6.direct_links[0] && !sc6.direct_links[1]);
    assert(!sc6.direct_single[0] && sc6.direct_single[1] == &sc6.links[11]);
    assert(!tcg_supercache_link_direct(&cpu, tb(0), 0, tb(1), 0x100));
    assert(!tcg_supercache_link_direct(&cpu, tb(5), 1, tb(1), 0x100));
    assert(!calls);
    sc6.links[0].slot = UINT32_MAX;
    assert(tcg_supercache_link_direct(&cpu, tb(0), 1, tb(1), 0x100));
    cleanup();
    return 0;
}
"""


@pytest.mark.skipif(sys.platform != "linux", reason="the SC6 runtime requires Linux")
def test_direct_slot_index_routes_and_retains_runtime_gates(tmp_path):
    cc = shutil.which("cc")
    glib = ctypes.util.find_library("glib-2.0")
    if not cc or not glib:
        pytest.skip("requires a C compiler and the GLib runtime")
    source = tmp_path / "direct-index.c"
    binary = tmp_path / "direct-index"
    functions = "\n\n".join(added_function(name) for name in
                              ("sc6_index_direct_links", "tcg_supercache_link_direct"))
    source.write_text(HARNESS.replace("@FUNCTIONS@", functions))
    glib_arg = glib if Path(glib).is_absolute() else "-l:" + glib
    subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2",
                    str(source), "-o", str(binary), glib_arg], check=True,
                   capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)
