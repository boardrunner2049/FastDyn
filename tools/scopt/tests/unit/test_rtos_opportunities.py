from scopt.analysis.rtos_opportunities import module_for, tb_entries


def test_classify_actual_kernel_source():
    d = "fixture/FreeRTOS/Demo/app/build/gcc"
    assert module_for(d, "../../../../Source/list.c") == "lists"
    assert module_for(d, "../../list.c") is None
    assert (
        module_for("/", "FreeRTOS-Kernel/stream_buffer.c") == "stream_message_buffers"
    )
    assert module_for("src", "kernel/portable/MemMang/heap_3.c", "src/kernel") == "heap"


def test_sum_entry_translations_only(tmp_path):
    p = tmp_path / "profile"
    p.write_text("TB 9a 2 3 0 9c 2 70470000\nEDGE 9a 9c 3\nTB 9a 4 1 0 a0 2 70470000\n")
    assert tb_entries(p) == {0x9A: 4}
