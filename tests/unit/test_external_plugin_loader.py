"""The native loader for out-of-tree plugins (virtuals/runtime_sdk.c), compiled
with gcc and driven with the test plugin from tests/plugins/mmio_count."""

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

# Stand-ins for what runtime_sdk.c uses from FastDyn's core and from QEMU,
# kept apart from the SDK headers: only the symbol names have to match.
STUBS = r"""
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef void (*exit_fn)(void);
exit_fn exit_hooks[8];
int exit_hook_count;
const char *artifact_root;

void core_register_exit_hook(exit_fn cb) { exit_hooks[exit_hook_count++] = cb; }
void core_register_tb_trans_hook(void *cb) { (void)cb; }
void core_register_irq_hook(void *a, void *b) { (void)a; (void)b; }
int core_get_run_artifact_path(const char *relative, char *out, size_t size) {
    return snprintf(out, size, "%s/%s", artifact_root, relative) >= (int)size ? -1 : 0;
}
uint64_t core_get_pc(void) { return 0; }
uint64_t core_get_sp(void) { return 0; }
uint64_t core_get_icount(void) { return 0; }
void core_wait_for_trace_drain(void) {}
int core_register_virtual_rule(void) { return 0; }
int core_register_register_update(void) { return 0; }
int core_register_gated_modifier(void) { return 0; }
uint32_t qemu_get_register(int r) { (void)r; return 0; }
void qemu_set_register(void) {}
void *qemu_plugin_get_registers(void) { return NULL; }
int qemu_plugin_read_register(void) { return -1; }
void qemu_plugin_set_register(void) {}
int qemu_plugin_read_memory(void) { return -1; }
int qemu_plugin_write_memory(void) { return -1; }
void qemu_plugin_raise_irq(void) {}
int64_t qemu_plugin_get_virtual_timer(void) { return 0; }
int virtual_register(void) { return 0; }
"""

HARNESS = r"""
#include <fastdyn_runtime.h>
#include <virtuals/mmio_observer.h>
#include <stdio.h>

extern VirtualExitCallback exit_hooks[8];
extern int exit_hook_count;
extern const char *artifact_root;

/* One compiled-in plugin, as in libfastdyn.so. */
static int builtin_initialize(const VirtualContext *ctx) { (void)ctx; return 0; }
VIRTUAL_PLUGIN("builtin", builtin_initialize);

int main(int argc, char **argv) {
    artifact_root = argv[1];
    virtual_initialize_plugins();
    if (virtual_load_external_plugins(argv[2]) != 0) {
        return 3;
    }
    VirtualMmioAccess reads[] = {{.handled = true}, {.handled = true}, {.handled = false}};
    VirtualMmioAccess write = {.is_write = true, .handled = true};
    for (int i = 0; i < 3; i++) virtual_dispatch_mmio(&reads[i]);
    virtual_dispatch_mmio(&write);
    for (int i = 0; i < exit_hook_count; i++) exit_hooks[i]();
    puts("ok");
    return 0;
}
"""

BAD_PLUGINS = {
    "nodesc": "int unrelated(void) { return 0; }",
    "abi": 'static int i(const VirtualContext *c) { return 0; }\n'
           '__attribute__((visibility("default"))) const VirtualExternalPlugin fastdyn_external_plugin = '
           '{2, sizeof(VirtualExternalPlugin), "bad", i};',
    "noname": 'static int i(const VirtualContext *c) { return 0; }\n'
              '__attribute__((visibility("default"))) const VirtualExternalPlugin fastdyn_external_plugin = '
              '{VIRTUAL_PLUGIN_ABI_VERSION, sizeof(VirtualExternalPlugin), "", i};',
    "fails": 'static int i(const VirtualContext *c) { return -1; }\nFASTDYN_EXTERNAL_PLUGIN("fails", i);',
    # Uses the SDK (as real plugins do), but resolves it from another library.
    "otherlib": 'static int i(const VirtualContext *c) { return virtual_register_callback(c, "x", 0); }\n'
                'FASTDYN_EXTERNAL_PLUGIN("otherlib", i);',
    "clash": 'static int i(const VirtualContext *c) { return 0; }\nFASTDYN_EXTERNAL_PLUGIN("builtin", i);',
}


def _gcc(*args):
    glib = subprocess.run(["pkg-config", "--cflags", "--libs", "glib-2.0"], capture_output=True, text=True,
                          check=True).stdout.split()
    result = subprocess.run(["gcc", "-std=gnu11", "-g", f"-I{REPO / 'include'}", f"-I{QEMU_INCLUDE}",
                             *map(str, args), *glib], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("loader")
    (tmp / "harness.c").write_text(HARNESS)
    (tmp / "stubs.c").write_text(STUBS)
    harness = tmp / "harness"
    _gcc(tmp / "harness.c", tmp / "stubs.c", REPO / "virtuals" / "runtime_sdk.c", REPO / "utils" / "log.c",
         "-rdynamic", "-ldl", "-o", harness)
    libs = {"good": tmp / "mmio_count.so"}
    _gcc(REPO / "tests" / "plugins" / "mmio_count" / "runtime" / "mmio_count.c", "-shared", "-fPIC",
         "-o", libs["good"])
    # A library that also defines the SDK, standing in for another libfastdyn.so.
    (tmp / "fake.c").write_text("int virtual_register_callback(void) { return 0; }\n")
    _gcc(tmp / "fake.c", "-shared", "-fPIC", "-o", tmp / "libfakefastdyn.so")
    for name, body in BAD_PLUGINS.items():
        (tmp / f"{name}.c").write_text("#include <fastdyn_runtime.h>\n" + body + "\n")
        extra = [f"-L{tmp}", "-lfakefastdyn", f"-Wl,-rpath,{tmp}"] if name == "otherlib" else []
        libs[name] = tmp / f"{name}.so"
        _gcc(tmp / f"{name}.c", "-shared", "-fPIC", "-o", libs[name], *extra)
    return harness, libs


def _run(harness, artifacts, paths):
    return subprocess.run([str(harness), str(artifacts), paths], capture_output=True, text=True)


def test_a_plugin_is_loaded_and_works_like_a_built_in_one(built, tmp_path):
    harness, libs = built
    (tmp_path / "mmio_count").mkdir()
    (tmp_path / "mmio_count" / "settings.txt").write_text("unit\n")

    result = _run(harness, tmp_path, str(libs["good"]))

    assert result.returncode == 0, result.stderr
    assert f"fastdyn.plugins|INFO|  loaded plugin 'mmio_count' from {libs['good']}" in result.stderr
    assert "fastdyn.mmio_count|INFO|  counting MMIO accesses (unit)" in result.stderr
    counts = (tmp_path / "mmio_count" / "counts.txt").read_text()
    assert "label=unit\nreads_handled=2\nreads_unhandled=1\nwrites_handled=1\nwrites_unhandled=0\n" in counts


@pytest.mark.parametrize(
    "paths, message",
    [
        ("/nonexistent/plugin.so", "cannot load plugin library /nonexistent/plugin.so"),
        ("{nodesc}", "{nodesc} is not a FastDyn plugin (no fastdyn_external_plugin"),
        ("{abi}", "{abi} was built for plugin ABI 2, but this FastDyn expects 1"),
        ("{noname}", "{noname}: its descriptor has no name or initializer"),
        ("{fails}", "plugin 'fails' ({fails}) failed to initialize"),
        ("{otherlib}", "{otherlib} is linked against a different libfastdyn.so than the one running"),
        ("{good}:{good}", "{good}: a plugin named 'mmio_count' is already loaded"),
        ("{clash}", "{clash}: a plugin named 'builtin' is already loaded"),
    ],
)
def test_libraries_that_cannot_be_used_are_reported(built, tmp_path, paths, message):
    harness, libs = built
    names = {name: str(path) for name, path in libs.items()}

    result = _run(harness, tmp_path, paths.format(**names))

    assert result.returncode == 3
    assert f"fastdyn.plugins|ERROR|  {message.format(**names)}" in result.stderr
