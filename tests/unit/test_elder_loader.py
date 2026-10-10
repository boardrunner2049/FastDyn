"""Elder's model loader (device_models/elder/elder.c), compiled with gcc and
driven through its callbacks with stub dev_* functions."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
QEMU_INCLUDE = REPO / "third_party" / "common" / "qemu" / "include"

pytestmark = pytest.mark.skipif(
    shutil.which("gcc") is None or shutil.which("pkg-config") is None or not QEMU_INCLUDE.is_dir(),
    reason="needs gcc, pkg-config and the QEMU submodule",
)

# An older model: <device>_init/_read/_write, no fastdyn_model.
LEGACY_MODEL = r"""
#include <device.h>
static int interrupts;
void *legacy_init(ConfigSection *section) { (void)section; return &interrupts; }
uint64_t legacy_read(void *opaque, hwaddr addr, unsigned size, uint64_t pc) {
    return (addr & 0xFF) == 4 ? (uint64_t)*(int *)opaque : 0x55;
}
void legacy_write(void *opaque, hwaddr addr, uint64_t value, unsigned size, uint64_t pc) {}
int legacy_interrupt(int irq) { interrupts++; return 0; }
"""

# Descriptor variants that elder must refuse.
BAD_MODELS = {
    "abi": "const FdModel fastdyn_model = {.abi = 1, .size = sizeof(FdModel), .init = i, .read = r, .write = w};",
    "noread": "const FdModel fastdyn_model = {FASTDYN_MODEL_ABI_FIELDS, .init = i, .write = w};",
    "nullinit": "const FdModel fastdyn_model = {FASTDYN_MODEL_ABI_FIELDS, .init = n, .read = r, .write = w};",
}
BAD_MODEL_PREFIX = r"""
#include <fastdyn/device_model.h>
static void *i(const FdDevice *d, const char *c, const FdHost *h) { return (void *)d; }
static void *n(const FdDevice *d, const char *c, const FdHost *h) { return 0; }
static uint64_t r(void *o, hwaddr a, unsigned s, uint64_t pc) { return 0; }
static void w(void *o, hwaddr a, uint64_t v, unsigned s, uint64_t pc) {}
"""

HARNESS = r"""
#include <device.h>
#include <utils.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern DeviceModel elder_model_def;

/* --- stubs for what elder uses from the rest of FastDyn --- */
static int range_registrations, irq_registrations[8], irq_count;
void utils_die(const char *msg) { fprintf(stderr, "DIE: %s\n", msg); exit(3); }
void utils_parse_ranges(int n, const AddrRange *in, Range *out) {
    for (int i = 0; i < n; i++) { out[i].start = in[i].start; out[i].end = in[i].end; }
}
void dev_register_device_model(hwaddr start, hwaddr end, DeviceModel *dev) { range_registrations++; }
void dev_register_interrupt_device_model(int irq, DeviceModel *dev) { irq_registrations[irq_count++] = irq; }

#define CHECK(cond) do { if (!(cond)) { fprintf(stderr, "check failed line %d: %s\n", __LINE__, #cond); exit(1); } } while (0)

/* argv: one "name|library|start|end|irq|config" per device */
int main(int argc, char **argv) {
    int n = argc - 1;
    DeviceModels *devs = calloc(n, sizeof(DeviceModels));
    AddrRange *overall = calloc(n, sizeof(AddrRange));
    for (int i = 0; i < n; i++) {
        char *spec = strdup(argv[i + 1]);
        devs[i].name = strtok(spec, "|");
        devs[i].scroll_path = strtok(NULL, "|");
        devs[i].ranges = calloc(1, sizeof(AddrRange));
        devs[i].ranges[0].start = strtoull(strtok(NULL, "|"), NULL, 0);
        devs[i].ranges[0].end = strtoull(strtok(NULL, "|"), NULL, 0);
        devs[i].range_count = 1;
        devs[i].irqs = calloc(1, sizeof(uint16_t));
        devs[i].irqs[0] = (uint16_t)strtoul(strtok(NULL, "|"), NULL, 0);
        devs[i].irq_count = 1;
        devs[i].config_json = strtok(NULL, "|");
        overall[i] = devs[i].ranges[0];
    }
    ConfigSection section = {.name = "elder", .overall_ranges = overall,
                             .overall_range_count = n, .devices = devs, .device_count = n};
    elder_model_def.init(&section);
    if (n != 3) { puts("loaded"); return 0; }  /* the refusal cases stop before this */

    /* echo_a 0x40000000-0x400000FF irq 20, echo_b 0x40001000-0x400010FF irq 21, legacy irq 22 */
    CHECK(range_registrations == 3);
    CHECK(irq_count == 3 && irq_registrations[0] == 20 && irq_registrations[2] == 22);

    DeviceReadFunc rd = elder_model_def.read;
    DeviceWriteFunc wr = elder_model_def.write;
    CHECK(rd(NULL, 0x40000000, 4, 0) == 17);            /* each instance got its own config */
    CHECK(rd(NULL, 0x40001000, 4, 0) == 34);
    wr(NULL, 0x40000000, 0x111, 4, 0);
    wr(NULL, 0x40001000, 0x222, 4, 0);
    CHECK(rd(NULL, 0x40000000, 4, 0) == 0x111);         /* ... and its own state */
    CHECK(rd(NULL, 0x40001000, 4, 0) == 0x222);
    CHECK(rd(NULL, 0x400000FF, 1, 0) == 0xAA);          /* the last byte of a range is routed */

    elder_model_def.interrupt(20);
    elder_model_def.interrupt(21);
    elder_model_def.interrupt(21);
    elder_model_def.serve(20);
    CHECK(rd(NULL, 0x40000004, 4, 0) == 1);             /* echo_a: only its own IRQ 20 */
    CHECK(rd(NULL, 0x40001004, 4, 0) == 2);             /* echo_b: only its own IRQ 21 */
    CHECK(rd(NULL, 0x40000008, 4, 0) == 1 && rd(NULL, 0x40001008, 4, 0) == 0);
    CHECK(rd(NULL, 0x40002000, 4, 0) == 0x55);          /* the older model still works ... */
    CHECK(rd(NULL, 0x40002004, 4, 0) == 3);             /* ... and still hears every elder IRQ */
    puts("ok");
    return 0;
}
"""


def _flags():
    glib = subprocess.run(["pkg-config", "--cflags", "glib-2.0"], capture_output=True, text=True, check=True)
    return ["-std=gnu11", "-g", f"-I{REPO / 'include'}", f"-I{QEMU_INCLUDE}",
            f"-I{REPO / 'device_models' / 'elder'}", *glib.stdout.split()]


def _compile(tmp_path, name, sources, *extra):
    out = tmp_path / name
    result = subprocess.run(["gcc", *_flags(), *map(str, sources), "-o", str(out), *extra],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return out


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("elder")
    (tmp / "legacy.c").write_text(LEGACY_MODEL)
    (tmp / "harness.c").write_text(HARNESS)
    libs = {
        "echo": _compile(tmp, "elder_echo.so", [REPO / "tests" / "models" / "elder_echo.c"], "-shared", "-fPIC"),
        "legacy": _compile(tmp, "legacy.so", [tmp / "legacy.c"], "-shared", "-fPIC"),
    }
    for name, descriptor in BAD_MODELS.items():
        (tmp / f"{name}.c").write_text(BAD_MODEL_PREFIX + descriptor)
        libs[name] = _compile(tmp, f"{name}.so", [tmp / f"{name}.c"], "-shared", "-fPIC")
    harness = _compile(tmp, "harness", [tmp / "harness.c", REPO / "device_models" / "elder" / "elder.c",
                                        REPO / "utils" / "log.c"], "-ldl")
    return harness, libs


def _run(harness, *devices):
    return subprocess.run([str(harness), *devices], capture_output=True, text=True)


def test_one_model_serves_two_devices_next_to_an_older_model(built):
    harness, libs = built

    result = _run(
        harness,
        f'echo_a|{libs["echo"]}|0x40000000|0x400000FF|20|{{"reset": 17}}',
        f'echo_b|{libs["echo"]}|0x40001000|0x400010FF|21|{{"reset": 34}}',
        f'legacy|{libs["legacy"]}|0x40002000|0x400020FF|22|{{}}',
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "ok"
    # FdHost.log goes through FastDyn's logger, named after the device.
    assert "fastdyn.echo_a|INFO|  echo at 0x40000000, reset 0x11" in result.stderr
    assert "fastdyn.echo_b|INFO|  echo at 0x40001000, reset 0x22" in result.stderr


@pytest.mark.parametrize(
    "model, config, message",
    [
        ("abi", "{}", "dev0: {lib} was built for model ABI 1, but this FastDyn expects 2"),
        ("noread", "{}", "dev0: fastdyn_model.read is not set ({lib})"),
        ("nullinit", "{}", "dev0: the model refused its configuration (init returned NULL; {lib})"),
        ("echo", '{"refuse": true}', "dev0: the model refused its configuration (init returned NULL; {lib})"),
    ],
)
def test_models_elder_refuses(built, model, config, message):
    harness, libs = built

    result = _run(harness, f"dev0|{libs[model]}|0x40000000|0x400000FF|20|{config}")

    assert result.returncode == 3
    assert f"DIE: {message.format(lib=libs[model])}" in result.stderr
