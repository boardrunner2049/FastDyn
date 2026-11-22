"""Render this archived application's reserved evaluation with the campaign reporter."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "lib"))
from final_evaluation import table
from scopt.run.launcher import file_sha256


def local_file(name):
    path = HERE / name
    return {"path": str(path), "sha256": file_sha256(path)}


if __name__ == "__main__":
    protocol = json.loads((HERE / "protocol.json").read_text())
    case = {"application": protocol["application"], "family": protocol["family"], "status": "admitted",
            "weight": 1, "protocol": local_file("protocol.json"), "final": local_file("final.json")}
    print(table({"state": "completed", "applications": [case]}))
