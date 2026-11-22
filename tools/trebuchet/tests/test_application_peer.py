"""Exercise UART attachment lifecycle and binding without measuring performance."""
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
pytest.importorskip("scopt")
from application_peer import uart_peer
from scopt.run.launcher import file_sha256


PEER_SOURCE = """
import argparse, json, socket
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('peer')
for key in ('socket', 'stage', 'out', 'timeout'):
    p.add_argument('--' + key)
args = p.parse_args()
out = Path(args.out)
out.mkdir()
with socket.socket(socket.AF_UNIX) as server:
    server.bind(args.socket)
    server.listen()
    conn, _ = server.accept()
    with conn:
        verdict = 'PASS' if conn.recv(5) == b'valid' else 'FAIL'
        stage = json.loads(Path(args.stage).read_text())
        (out / 'result.json').write_text(json.dumps(dict(case=stage['case'], verdict=verdict)))
"""


@pytest.mark.parametrize("payload", [b"valid", b"wrong"])
def test_peer_checks_result_and_cleans_endpoint(tmp_path, payload):
    program = tmp_path / "peer.py"
    program.write_text(PEER_SOURCE)
    stage = tmp_path / "stage.json"
    stage.write_text(json.dumps({"case": "attachment-fixture"}))
    bank = tmp_path / "input"
    bank.write_bytes(b"attachment input")
    def pin(path):
        return {"path": str(path), "sha256": file_sha256(path)}
    spec = dict(program=pin(program), stage=pin(stage), bank="main", input_sha256=file_sha256(bank))
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        endpoint = Path(directory) / "uart"
        rd = SimpleNamespace(root=tmp_path, config=lambda: {"Machine": {"serial": "unix:" + str(endpoint)}})
        def attach():
            return uart_peer(spec)(rd, {"main": bank}, "run.json", 5)
        with attach() as collect:
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(endpoint))
                client.sendall(payload)
            doc = {"exit_code": 0, "timed_out": False}
            if payload == b"valid":
                result = collect(doc)
                assert result["receipt_sha256"] == file_sha256(result["receipt"])
            else:
                with pytest.raises(ValueError, match="oracle failed"):
                    collect(doc)
        assert not endpoint.exists()
        with pytest.raises(ValueError, match="existing peer evidence"), attach():
            pass
        bank.write_bytes(b"changed")
        with pytest.raises(ValueError, match="different scenario input"), attach():
            pass
