import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import stock


def test_bound_socket_may_refuse_before_qemu_listens(monkeypatch):
    channel = object()
    constructor = Mock(side_effect=[FileNotFoundError(), ConnectionRefusedError(), channel])
    monkeypatch.setattr(stock, "Gdb", constructor)
    assert stock.connect_gdb("gdb.sock", Mock(poll=lambda: None),
                             time.monotonic_ns() + 1_000_000_000) is channel


def test_qemu_exit_is_reported(monkeypatch):
    monkeypatch.setattr(stock, "Gdb", Mock(side_effect=ConnectionRefusedError()))
    with pytest.raises(RuntimeError, match="status 1"):
        stock.connect_gdb("gdb.sock", Mock(poll=lambda: 1), time.monotonic_ns())
