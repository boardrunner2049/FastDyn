"""include/fastdyn/device_model.h and fuzz_hooks.h, compiled with gcc."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INCLUDE = REPO / "include"
QEMU_INCLUDE = REPO / "third_party" / "common" / "qemu" / "include"

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not found")


@pytest.fixture(scope="module")
def qemu_flags():
    """device_model.h includes QEMU's qemu-plugin.h, which needs glib."""
    if shutil.which("pkg-config") is None or not QEMU_INCLUDE.is_dir():
        pytest.skip("needs pkg-config and the QEMU submodule")
    glib = subprocess.run(["pkg-config", "--cflags", "glib-2.0"], capture_output=True, text=True)
    if glib.returncode != 0:
        pytest.skip("glib-2.0 development files not found")
    return [f"-I{QEMU_INCLUDE}", *glib.stdout.split()]

MODEL = r"""
#include <fastdyn/device_model.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    uint64_t base;
    uint32_t data;
    char name[16];
} State;

static void *echo_init(const FdDevice *dev, const char *config_json, const FdHost *host) {
    (void)config_json;
    if (dev->size < sizeof(FdDevice) || dev->range_count != 1) {
        host->log(FD_LOG_LEVEL_ERROR, dev->name, "needs exactly one range");
        return NULL;
    }
    State *s = calloc(1, sizeof(*s));
    s->base = dev->ranges[0].base;
    strncpy(s->name, dev->name, sizeof(s->name) - 1);
    return s;
}

static uint64_t echo_read(void *opaque, hwaddr addr, unsigned size, uint64_t pc) {
    (void)size; (void)pc;
    State *s = opaque;
    return addr - s->base == 0 ? s->data : 0;
}

static void echo_write(void *opaque, hwaddr addr, uint64_t value, unsigned size, uint64_t pc) {
    (void)size; (void)pc;
    State *s = opaque;
    if (addr - s->base == 0) s->data = (uint32_t)value;
}

const FdModel fastdyn_model = {
    FASTDYN_MODEL_ABI_FIELDS,
    .init = echo_init,
    .read = echo_read,
    .write = echo_write,
};
"""

LOADER = r"""
#include <fastdyn/device_model.h>
#include <dlfcn.h>
#include <stdarg.h>
#include <stdio.h>

static void host_log(int level, const char *name, const char *format, ...) {
    va_list args;
    va_start(args, format);
    fprintf(stderr, "fastdyn.%s|%d|  ", name, level);
    vfprintf(stderr, format, args);
    fputc('\n', stderr);
    va_end(args);
}

int main(int argc, char **argv) {
    void *lib = dlopen(argv[1], RTLD_NOW);
    if (!lib) return 10;
    const FdModel *m = dlsym(lib, FASTDYN_MODEL_SYMBOL);
    if (!m || m->abi != FASTDYN_MODEL_ABI_VERSION || m->size != sizeof(FdModel)) return 11;
    if (!m->init || !m->read || !m->write || m->serve || m->interrupt) return 12;

    FdHost host = {sizeof(FdHost), host_log};
    FdRange r2 = {0x40004400, 0x400}, r3 = {0x40004800, 0x400};
    FdDevice usart2 = {sizeof(FdDevice), "usart2", &r2, 1, NULL, 0};
    FdDevice usart3 = {sizeof(FdDevice), "usart3", &r3, 1, NULL, 0};
    void *a = m->init(&usart2, "{}", &host);
    void *b = m->init(&usart3, "{}", &host);
    if (!a || !b || a == b) return 13;

    m->write(a, 0x40004400, 0x11, 4, 0);
    m->write(b, 0x40004800, 0x22, 4, 0);
    if (m->read(a, 0x40004400, 4, 0) != 0x11 || m->read(b, 0x40004800, 4, 0) != 0x22) return 14;

    FdDevice bad = {sizeof(FdDevice), "bad", NULL, 0, NULL, 0};
    if (m->init(&bad, "{}", &host) != NULL) return 15;
    puts("ok");
    return 0;
}
"""


def _gcc(tmp_path, source, *args, name="out", check=True):
    src = tmp_path / f"{name}.c"
    src.write_text(source)
    out = tmp_path / name
    result = subprocess.run(
        ["gcc", "-std=gnu11", "-Wall", "-Werror", "-Werror=incompatible-pointer-types",
         f"-I{INCLUDE}", str(src), "-o", str(out), *args],
        capture_output=True, text=True,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return out, result


def test_a_descriptor_model_serves_two_devices(tmp_path, qemu_flags):
    model, _ = _gcc(tmp_path, MODEL, *qemu_flags, "-shared", "-fPIC", name="echo.so")
    loader, _ = _gcc(tmp_path, LOADER, *qemu_flags, "-ldl", name="loader")

    result = subprocess.run([str(loader), str(model)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    assert "fastdyn.bad|40|  needs exactly one range" in result.stderr


MINIMAL = """
#include <fastdyn/device_model.h>
%(init)s { %(init_body)s }
%(read)s { return 0; }
static void w(void *o, hwaddr addr, uint64_t value, unsigned size, uint64_t pc) {}
const FdModel fastdyn_model = {FASTDYN_MODEL_ABI_FIELDS, .init = i, .read = r, .write = w};
"""
INIT = "static void *i(const FdDevice *dev, const char *cfg, const FdHost *host)"
READ = "static uint64_t r(void *o, hwaddr addr, unsigned size, uint64_t pc)"


@pytest.mark.parametrize(
    "init, init_body, read, compiles",
    [
        (INIT, "return 0;", READ, True),
        # read without the pc argument (a mismatch generated models had)
        (INIT, "return 0;", "static uint64_t r(void *o, hwaddr addr, unsigned size)", False),
        # init returning nothing
        ("static void i(const FdDevice *dev, const char *cfg, const FdHost *host)", "", READ, False),
    ],
)
def test_the_compiler_checks_signatures(tmp_path, qemu_flags, init, init_body, read, compiles):
    source = MINIMAL % {"init": init, "init_body": init_body, "read": read}

    _, result = _gcc(tmp_path, source, *qemu_flags, "-fPIC", "-c", name="model.o", check=False)

    assert (result.returncode == 0) is compiles, result.stderr
    if not compiles:
        assert "incompatible" in result.stderr


def test_header_works_with_the_internal_and_log_headers(tmp_path, qemu_flags):
    source = "#include <device.h>\n#include <fastdyn/log.h>\n#include <fastdyn/device_model.h>\n" \
             "static hwaddr h;\nint main(void) { return (int)h + FD_LOG_LEVEL_INFO - 20; }\n"

    _gcc(tmp_path, source, *qemu_flags, "-c", name="both.o")


def test_the_host_functions_come_with_the_header(tmp_path, qemu_flags):
    source = (
        "#include <fastdyn/device_model.h>\n"
        "static void tick(void *opaque) { (void)opaque; qemu_plugin_raise_irq(54, 0); }\n"
        "uint64_t arm(void) {\n"
        "    uint64_t t = qemu_plugin_timer_new_ns(tick, NULL);\n"
        "    qemu_plugin_timer_alarm(t, (uint64_t)qemu_plugin_get_virtual_timer() + 1000);\n"
        "    return t;\n"
        "}\n"
    )
    _gcc(tmp_path, source, *qemu_flags, "-fPIC", "-c", name="host.o")


def test_fuzz_hooks_header_compiles_on_its_own(tmp_path):
    source = (
        "#include <fastdyn/fuzz_hooks.h>\n"
        "static void on_input(void) {}\n"
        "void model_setup(void) {\n"
        "    char buf[8];\n"
        "    fuzz_register_input_callback(on_input);\n"
        "    fuzz_set_data(buf, fuzz_get_data(buf, sizeof(buf)));\n"
        "    fuzz_request_sync(0x08000268u);\n"
        "}\n"
    )
    _gcc(tmp_path, source, "-fPIC", "-c", name="hooks.o")
