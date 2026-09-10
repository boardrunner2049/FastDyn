"""Track a trial's descendants, including helpers that start separate sessions."""
from pathlib import Path
import os
import signal
import subprocess
import time


def process_info(pid):
    try:
        # comm can contain spaces and parentheses. Fields after its final ')'
        # start with state; starttime distinguishes a reused PID.
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[1]), int(fields[19]), fields[0]
    except (OSError, ValueError, IndexError):
        return None


class ExperimentProcess:
    def __init__(self, command, **kwargs):
        self.process = subprocess.Popen(command, start_new_session=True, **kwargs)
        self.owned = {}
        self.poll()

    def poll(self):
        processes = {}
        for path in Path('/proc').iterdir():
            if path.name.isdigit() and (info := process_info(int(path.name))):
                processes[int(path.name)] = info
        parents = {self.process.pid} | set(self._alive())
        visited = set()
        while parents:
            children = set()
            for pid, (parent, started, _) in processes.items():
                if pid in parents or parent in parents:
                    if pid not in visited:
                        self.owned[pid] = started
                        children.add(pid)
                        visited.add(pid)
            parents = children
        return self.process.poll()

    def _alive(self):
        return [pid for pid, started in self.owned.items()
                if (info := process_info(pid)) and info[1] == started and info[2] != 'Z']

    def stop(self):
        self.poll()
        # Let FastDyn unwind its context managers and flush telemetry first.
        for sig, timeout in ((signal.SIGINT, 12), (signal.SIGTERM, 2), (signal.SIGKILL, 2)):
            for pid in self._alive():
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + timeout
            while self._alive() and time.monotonic() < deadline:
                self.process.poll()
                time.sleep(.1)
            if not self._alive():
                break
        self.process.wait(timeout=2)
        if self._alive():
            raise RuntimeError('Experiment descendants did not stop')
