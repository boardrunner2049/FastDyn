set pagination off
set confirm off
set startup-with-shell off
set disable-randomization off
set print thread-events off
set auto-solib-add off
set debuginfod enabled off
starti
python
import gdb, json, os
from pathlib import Path
source = Path(os.environ["PYE_PATH_COUNTER_PLAN"])
plan = json.loads(source.read_text())
observed = {}
hits = {key: 0 for key in plan["points"]}
errors = []
checked = {}
class PathCounter(gdb.Breakpoint):
    def __init__(self, label, point):
        self.label = label
        self.address_wanted = plan["base"] + point["offset"]
        self.prefix = bytes.fromhex(point["expected_hex"])
        super().__init__("*%#x" % self.address_wanted, type=gdb.BP_HARDWARE_BREAKPOINT, internal=True)
        self.silent = True
    def stop(self):
        # Check the actual mapped code the first time this physical path executes.
        if self.label not in checked:
            live = bytes(gdb.selected_inferior().read_memory(self.address_wanted, len(self.prefix)))
            if live != self.prefix:
                errors.append("live instruction differs at " + self.label)
                return True
            checked[self.label] = live.hex()
        hits[self.label] += 1
        return False
counters = {key: PathCounter(key, value) for key, value in plan["points"].items()}
def on_exit(event):
    observed["exit_code"] = getattr(event, "exit_code", None)
    observed["counts"] = hits
    observed["errors"] = errors
    observed["checked"] = checked
    (source.parent / "counts.json").write_text(json.dumps(observed, indent=2) + "\n")
gdb.events.exited.connect(on_exit)
end
continue
