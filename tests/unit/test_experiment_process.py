import importlib.util
from pathlib import Path
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location(
    "experiment_process", Path(__file__).resolve().parents[2] / "utils/experiment_process.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_stops_separate_session_helper_and_preserves_unrelated_process(tmp_path):
    pid_file = tmp_path / 'helper.pid'
    child = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(90)'
    command = [sys.executable, '-c',
        'import pathlib,subprocess,sys,time; '
        f'p=subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); '
        f'pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(90)']
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
    trial = module.ExperimentProcess(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        helper = int(pid_file.read_text())
        trial.poll()
        assert helper in trial.owned
        trial.stop()
        assert not trial._alive()
        assert unrelated.poll() is None
    finally:
        trial.stop()
        unrelated.terminate()
        unrelated.wait(timeout=5)
